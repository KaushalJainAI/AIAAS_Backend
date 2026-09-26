"""
Evaluating the formulas a workbook stores — for the app, the preview and the
benchmark, from one implementation.

Grown from `eval/office_files.py` (safe AST, no `eval`): xlsxwriter writes
formulas without cached values, so a reader asking openpyxl for `data_only`
values sees nothing, and the formula text alone says only that *some* formula
is there. `evaluate` computes the common subset instead, and anything outside
it is reported as not evaluable rather than guessed.

The language: cell and range references (also across sheets), `+ - * / ^`,
comparisons (`= <> < <= > >=`), `&` concatenation, and SUM / AVERAGE / MIN /
MAX / COUNT / COUNTA / ROUND / ABS / IF / IFERROR / AND / OR / NOT / CONCAT /
LEN / UPPER / LOWER / TRIM / TODAY / SUMIF / COUNTIF / AVERAGEIF / VLOOKUP /
XLOOKUP. `eval/office_files.py` re-exports this module, so the benchmark and
the app agree on every value.

Safety, as before: a model-written string is evaluated inside this process, so
the expression is parsed to an AST and every node is checked against a closed
list before anything runs — there are no builtins in its namespace, and the
tree is interpreted node by node rather than compiled. Omitted lookups default
to exact match: an approximate VLOOKUP on unsorted data answers confidently
and wrongly, while an exact one fails loudly.
"""
from __future__ import annotations

import ast
import datetime
import fnmatch
import io
import re
from typing import Any


class FormulaError(ValueError):
    """The formula uses something outside the evaluable subset."""


def workbook(data: bytes):
    import openpyxl

    return openpyxl.load_workbook(io.BytesIO(data))  # formulas, not cached values


def has_chart(wb, sheet: str | None = None) -> bool:
    sheets = [wb[sheet]] if sheet else wb.worksheets
    return any(getattr(ws, '_charts', None) for ws in sheets)


_SHEET = r"(?:'(?P<qs>[^']+)'|(?P<s>[A-Za-z_][\w.]*))!"
_CELL = r'\$?[A-Z]{1,3}\$?\d+'
#: Both anchored on the left, so `LOG10(` is not read as a reference to `OG10`
#: and `Data!A1` is not read a second time as a bare `A1`.
_RANGE_RE = re.compile(rf"(?<![\w.!']){'(?:' + _SHEET + ')'}?(?P<a>{_CELL}):(?P<b>{_CELL})")
_REF_RE = re.compile(rf"(?<![\w.!']){'(?:' + _SHEET + ')'}?(?P<a>{_CELL})(?![\w(])")
_FUNCS = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTA', 'ROUND', 'ABS',
          'IF', 'IFERROR', 'AND', 'OR', 'NOT', 'CONCAT',
          'LEN', 'UPPER', 'LOWER', 'TRIM', 'TODAY',
          'SUMIF', 'COUNTIF', 'AVERAGEIF', 'VLOOKUP', 'XLOOKUP'}
_LAZY = {'IF', 'IFERROR', 'AND', 'OR', 'NOT'}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
                  ast.IfExp, ast.Call, ast.Name, ast.Load,
                  ast.Constant, ast.List,
                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.BitAnd,
                  ast.USub, ast.UAdd, ast.Not,
                  ast.And, ast.Or,
                  ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)

#: Nesting of one evaluation. Chains longer than this (a running total down a
#: column) never reach it: `cell` evaluates a cell's dependencies first, in
#: dependency order, so each step finds its inputs already computed.
_MAX_DEPTH = 30
#: Cells one range may read *inside the sheet's used area* (rows up to the
#: last filled row, at the range's full width). A range this size or smaller
#: is read whole, empty tail included; a bigger one that only runs past the
#: data (`=SUM(A1:Z1048576)` over 50 rows) drops its empty trailing rows —
#: the same answer for everything except blank-matching criteria, and the
#: alternative was 26M cell objects on a 913 MB box. Real data over the cap
#: is refused.
_MAX_RANGE_CELLS = 200_000


class _TooDeep(FormulaError):
    """Depth is a property of one path, never of the cell: not memoised."""


def _key(sheet: str, ref: str) -> tuple[str, str]:
    return (sheet.casefold(), ref.replace('$', '').upper())


def _memo(wb) -> dict:
    """Computed values, kept on the workbook object for its lifetime.

    Every caller opens a workbook, reads it, and closes it; nothing mutates
    one after evaluating it. An entry also records the formula text it was
    computed from and is ignored when that cell's text has changed; after
    changing *other* cells, call `forget(wb)`.
    """
    memo = getattr(wb, '_aiaas_formula_memo', None)
    if memo is None:
        memo = {}
        wb._aiaas_formula_memo = memo
    return memo


def forget(wb) -> None:
    """Drop computed values (after mutating the workbook)."""
    wb._aiaas_formula_memo = {}


def _raw(ws, row: int, col: int) -> Any:
    """A cell's stored value without creating the cell (`ws[...]` creates)."""
    found = ws._cells.get((row, col))
    return None if found is None else found.value


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith('=')


def _coord(ref: str) -> tuple[int, int]:
    from openpyxl.utils.cell import coordinate_to_tuple

    return coordinate_to_tuple(ref.replace('$', '').upper())


def _bounds(ws, a: str, b: str) -> tuple[int, int, int, int]:
    """(min_row, min_col, max_row, max_col), refusing a range that would read
    more than `_MAX_RANGE_CELLS` cells of the sheet's used area."""
    r1, c1 = _coord(a)
    r2, c2 = _coord(b)
    r1, r2 = min(r1, r2), max(r1, r2)
    c1, c2 = min(c1, c2), max(c1, c2)
    rows = max(0, min(r2, ws.max_row or 0) - r1 + 1)
    if rows * (c2 - c1 + 1) > _MAX_RANGE_CELLS:
        raise FormulaError(
            f'the range {a}:{b} covers more than {_MAX_RANGE_CELLS:,} filled-in '
            f'cells, which is more than is evaluated here'
        )
    return r1, c1, r2, c2


def cell(wb, sheet: str, ref: str, _depth: int = 0, _seen: set | None = None) -> Any:
    """A cell's value, evaluating it when it holds a formula."""
    from openpyxl.utils.cell import get_column_letter

    row, col = _coord(ref)
    coord = f'{get_column_letter(col)}{row}'
    key = (sheet.casefold(), coord)
    seen = _seen if _seen is not None else set()
    if key in seen:
        raise FormulaError(f'{sheet}!{coord} refers to itself')
    value = _raw(wb[sheet], row, col)
    if not _is_formula(value):
        return value
    memo = _memo(wb)
    hit = memo.get(key)
    if hit is not None and hit[0] == value:
        if isinstance(hit[1], FormulaError):
            raise hit[1]
        return hit[1]
    if _depth == 0:
        _prime(wb, sheet, coord)
        hit = memo.get(key)
        if hit is not None and hit[0] == value:
            if isinstance(hit[1], FormulaError):
                raise hit[1]
            return hit[1]
    try:
        result = evaluate(wb, sheet, value, _depth + 1, seen | {key})
    except _TooDeep:
        raise
    except FormulaError as exc:
        memo[key] = (value, exc)
        raise
    memo[key] = (value, result)
    return result


def _dependencies(wb, sheet: str, formula: str) -> list[tuple[str, str]]:
    """The formula cells `formula` reads directly (sheet, coord)."""
    from openpyxl.utils.cell import get_column_letter

    out: list[tuple[str, str]] = []
    for part, is_code in _split_code(formula.lstrip('=')):
        if not is_code:
            continue
        for m in _RANGE_RE.finditer(part):
            name = m.group('qs') or m.group('s') or sheet
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            try:
                r1, c1, r2, c2 = _bounds(ws, m.group('a'), m.group('b'))
            except FormulaError:
                continue  # evaluation reports it
            for r in range(r1, min(r2, ws.max_row or 0) + 1):
                for c in range(c1, min(c2, ws.max_column or 0) + 1):
                    if _is_formula(_raw(ws, r, c)):
                        out.append((name, f'{get_column_letter(c)}{r}'))
        for m in _REF_RE.finditer(_RANGE_RE.sub(' ', part)):
            name = m.group('qs') or m.group('s') or sheet
            if name in wb.sheetnames:
                r, c = _coord(m.group('a'))
                if _is_formula(_raw(wb[name], r, c)):
                    out.append((name, f'{get_column_letter(c)}{r}'))
    return out


def _prime(wb, sheet: str, coord: str) -> None:
    """Evaluate what `coord` depends on, deepest first, into the memo.

    Iterative (no recursion), so a 5,000-row running total is 5,000 shallow
    evaluations instead of one 5,000-deep one. Cycles are skipped here and
    reported by the evaluation itself.
    """
    memo = _memo(wb)
    state: dict[tuple[str, str], int] = {}  # 1 = on the path, 2 = done
    order: list[tuple[str, str]] = []
    stack: list[tuple[str, str, bool]] = [(sheet, coord, False)]
    while stack:
        name, ref, done = stack.pop()
        key = (name.casefold(), ref)
        if done:
            state[key] = 2
            order.append((name, ref))
            continue
        if state.get(key):
            continue
        r, c = _coord(ref)
        raw = _raw(wb[name], r, c)
        hit = memo.get(key)
        if hit is not None and hit[0] == raw:
            state[key] = 2
            continue
        state[key] = 1
        stack.append((name, ref, True))
        for dep_name, dep_ref in _dependencies(wb, name, raw):
            if not state.get((dep_name.casefold(), dep_ref)):
                stack.append((dep_name, dep_ref, False))
    for name, ref in order[:-1]:  # the last is the cell itself
        try:
            cell(wb, name, ref, _depth=1)
        except FormulaError:
            pass  # memoised; the dependent re-raises it when it reads it


def evaluate(wb, sheet: str, formula: str, _depth: int = 0, _seen: set | None = None) -> Any:
    if _depth > _MAX_DEPTH:
        raise _TooDeep('formulas refer to each other too deeply (a cycle?)')
    seen = _seen if _seen is not None else set()
    body = formula.lstrip('=').strip()
    refs: list[tuple[str, str, str | None]] = []

    def hold(sheet_name: str, a: str, b: str | None) -> str:
        refs.append((sheet_name, a, b))
        return f'__r{len(refs) - 1}'

    def _range(m):
        return hold(m.group('qs') or m.group('s') or sheet, m.group('a'), m.group('b'))

    def _ref(m):
        return hold(m.group('qs') or m.group('s') or sheet, m.group('a'), None)

    # Transforms run on code spans only: `="a=b"` keeps its string.
    spans = _split_code(body)
    spans = [(_RANGE_RE.sub(_range, part) if is_code else part, is_code)
             for part, is_code in spans]
    spans = [(_REF_RE.sub(_ref, part) if is_code else part, is_code)
             for part, is_code in spans]
    body = ''.join(
        _translate_operators(part) if is_code else part for part, is_code in spans
    )
    try:
        tree = ast.parse(body, mode='eval')
    except SyntaxError as exc:
        raise FormulaError(f'cannot parse {formula!r}') from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(f'{formula!r} uses {type(node).__name__}, which is not evaluable here')
        if isinstance(node, ast.Name) and not (
                node.id in _FUNCS or node.id in ('TRUE', 'FALSE') or node.id.startswith('__r')):
            raise FormulaError(f'{formula!r} uses {node.id}, which is not evaluable here')
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise FormulaError(f'{formula!r} calls something outside the supported functions')

    names: dict[str, Any] = {}
    for i, (sheet_name, a, b) in enumerate(refs):
        if sheet_name not in wb.sheetnames:
            raise FormulaError(f'{formula!r} refers to a sheet that does not exist: {sheet_name}')
        if b is None:
            names[f'__r{i}'] = cell(wb, sheet_name, a, _depth, seen)
        else:
            names[f'__r{i}'] = _range_values(wb, sheet_name, a, b, _depth, seen)
    return _run(tree.body, names, formula, _depth)


def _range_values(wb, sheet: str, a: str, b: str, depth: int, seen: set) -> list[list]:
    """A range's values, row by row, without creating a cell object per
    coordinate: past the sheet's used area every value is empty."""
    from openpyxl.utils.cell import get_column_letter

    ws = wb[sheet]
    r1, c1, r2, c2 = _bounds(ws, a, b)
    last_row, last_col = ws.max_row or 0, ws.max_column or 0
    width = c2 - c1 + 1
    out: list[list] = []
    keep_tail = (r2 - r1 + 1) * width <= _MAX_RANGE_CELLS
    for r in range(r1, r2 + 1):
        if r > last_row:
            if keep_tail:
                out.extend([None] * width for _ in range(r2 - r + 1))
            break
        row: list = []
        for c in range(c1, min(c2, last_col) + 1):
            raw = _raw(ws, r, c)
            if _is_formula(raw):
                row.append(cell(wb, sheet, f'{get_column_letter(c)}{r}', depth, seen))
            else:
                row.append(raw)
        row.extend([None] * (width - len(row)))
        out.append(row)
    return out


def _split_code(body: str) -> list[tuple[str, bool]]:
    """Split into (span, is_code): quoted spans are never transformed."""
    parts: list[tuple[str, bool]] = []
    buf: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            if buf:
                parts.append((''.join(buf), True))
                buf = []
            j = i + 1
            lit = ['"']
            while j < n:
                if body[j] == '"' and body[j:j + 2] != '""':
                    lit.append('"')
                    j += 1
                    break
                lit.append(body[j])
                j += 1
            parts.append((''.join(lit), False))
            i = j
        else:
            buf.append(ch)
            i += 1
    if buf:
        parts.append((''.join(buf), True))
    return parts


def _translate_operators(code: str) -> str:
    code = code.replace('<>', '!=')
    # A lone `=` is equality (`=A1=5` means `A1 == 5`); `<=`, `>=`, `==`
    # and `!=` are left alone.
    code = re.sub(r'(?<![<>=!])=(?![=])', '==', code)
    return code.replace('^', '**')


# ---------------------------------------------------------------------------
# Interpretation (no `eval`: the tree is walked, and IF / IFERROR / AND / OR /
# NOT evaluate only the branches they take)
# ---------------------------------------------------------------------------

def _run(node: ast.AST, names: dict[str, Any], formula: str, depth: int) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise FormulaError(f'{formula!r} has a constant that is not evaluable here')
    if isinstance(node, ast.Name):
        if node.id == 'TRUE':
            return True
        if node.id == 'FALSE':
            return False
        if node.id in names:
            return names[node.id]
        raise FormulaError(f'{formula!r} uses {node.id}, which is not evaluable here')
    if isinstance(node, ast.BinOp):
        left, right = _run(node.left, names, formula, depth), _run(node.right, names, formula, depth)
        if isinstance(node.op, ast.BitAnd):
            return _display(left) + _display(right)
        return _arith(node.op, left, right, formula)
    if isinstance(node, ast.UnaryOp):
        value = _run(node.operand, names, formula, depth)
        if isinstance(node.op, ast.Not):
            return not _truthy(value)
        return _arith(node.op, 0, value, formula)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _run(v, names, formula, depth)
                if not _truthy(result):
                    return False
            return result
        result = False
        for v in node.values:
            result = _run(v, names, formula, depth)
            if _truthy(result):
                return True
        return result
    if isinstance(node, ast.Compare):
        left = _run(node.left, names, formula, depth)
        for op, comp in zip(node.ops, node.comparators):
            right = _run(comp, names, formula, depth)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _run(node.body if _truthy(_run(node.test, names, formula, depth))
                    else node.orelse, names, formula, depth)
    if isinstance(node, ast.Call):
        func = node.func.id if isinstance(node.func, ast.Name) else ''
        if func in _LAZY:
            return _lazy(func, node.args, names, formula, depth)
        args = [_run(a, names, formula, depth) for a in node.args]
        if func not in _EAGER:
            raise FormulaError(f'{formula!r} calls {func}, which is not evaluable here')
        try:
            return _EAGER[func](args, formula)
        except FormulaError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrong arity etc. is #VALUE!, not a crash
            raise FormulaError(f'{formula!r} misuses {func}') from exc
    raise FormulaError(f'{formula!r} uses {type(node).__name__}, which is not evaluable here')


def _lazy(func: str, args: list[ast.AST], names: dict[str, Any], formula: str, depth: int) -> Any:
    if func == 'IF':
        if len(args) not in (2, 3):
            raise FormulaError(f'{formula!r} misuses IF')
        branch = args[1] if _truthy(_run(args[0], names, formula, depth)) else (
            args[2] if len(args) == 3 else None)
        return False if branch is None else _run(branch, names, formula, depth)
    if func == 'IFERROR':
        if len(args) != 2:
            raise FormulaError(f'{formula!r} misuses IFERROR')
        try:
            return _run(args[0], names, formula, depth)
        except FormulaError:
            return _run(args[1], names, formula, depth)
    if func == 'AND':
        for a in args:
            if not _truthy(_run(a, names, formula, depth)):
                return False
        return True
    if func == 'OR':
        for a in args:
            if _truthy(_run(a, names, formula, depth)):
                return True
        return False
    if func == 'NOT':
        if len(args) != 1:
            raise FormulaError(f'{formula!r} misuses NOT')
        return not _truthy(_run(args[0], names, formula, depth))
    raise FormulaError(f'{formula!r} calls {func}, which is not evaluable here')  # pragma: no cover


def _arith(op, left: Any, right: Any, formula: str) -> float:
    try:
        if isinstance(op, ast.Add):
            return _num(left, formula) + _num(right, formula)
        if isinstance(op, ast.Sub):
            return _num(left, formula) - _num(right, formula)
        if isinstance(op, ast.Mult):
            return _num(left, formula) * _num(right, formula)
        if isinstance(op, ast.Div):
            divisor = _num(right, formula)
            if divisor == 0:
                raise FormulaError(f'{formula!r} divides by zero')
            return _num(left, formula) / divisor
        if isinstance(op, ast.Mod):
            return _num(left, formula) % _num(right, formula)
        if isinstance(op, ast.Pow):
            return _num(left, formula) ** _num(right, formula)
        if isinstance(op, (ast.USub,)):
            return -_num(right, formula)
        if isinstance(op, (ast.UAdd,)):
            return +_num(right, formula)
    except FormulaError:
        raise
    except Exception as exc:  # noqa: BLE001 — overflow etc. is #VALUE!, not a crash
        raise FormulaError(f'{formula!r} is not computable') from exc
    raise FormulaError(f'{formula!r} uses an operator that is not evaluable here')


def _num(value: Any, formula: str = '') -> float:
    """A value as a number. Blanks are 0 (as in arithmetic); anything else
    non-numeric is #VALUE! rather than a guess."""
    if value is None or value == '':
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.date):
        return float(value.toordinal())
    if isinstance(value, str):
        try:
            return float(value.replace(',', ''))
        except ValueError:
            pass
    raise FormulaError(f'{formula!r} needs a number, not {value!r}')


def _nums(values: list) -> list[float]:
    """The numbers in a flattened argument list; text and blanks are ignored,
    which is what makes SUM over a mixed range a sum rather than an error."""
    out = []
    for v in values:
        if isinstance(v, bool):
            out.append(float(v))
        elif isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, datetime.date):
            out.append(float(v.toordinal()))
    return out


def _flat(args) -> list:
    out: list = []
    for a in args:
        if isinstance(a, list):
            out.extend(_flat(a))
        else:
            out.append(a)
    return out


def _truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ''
    if isinstance(value, list):
        return len(value) > 0
    return True


def _display(value: Any) -> str:
    """A value as text for concatenation: integers without decimals."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _compare(op, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return _equals(left, right)
    if isinstance(op, ast.NotEq):
        return not _equals(left, right)
    order = _ordered(left, right)
    if order is None:
        return False
    left_n, right_n = order
    if isinstance(op, ast.Lt):
        return left_n < right_n
    if isinstance(op, ast.LtE):
        return left_n <= right_n
    if isinstance(op, ast.Gt):
        return left_n > right_n
    if isinstance(op, ast.GtE):
        return left_n >= right_n
    return False  # pragma: no cover


def _equals(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right or (isinstance(left, bool) and isinstance(right, bool) and left == right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, datetime.date) and isinstance(right, datetime.date):
        return left == right
    if (left is None or left == '') and (right is None or right == ''):
        return True
    return False


def _ordered(left: Any, right: Any) -> tuple[float, float] | None:
    """Two values on one scale, or None when they do not compare."""
    if isinstance(left, bool):
        left = float(left)
    if isinstance(right, bool):
        right = float(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left), float(right)
    if isinstance(left, str) and isinstance(right, str):
        return _str_order(left.casefold(), right.casefold())
    if isinstance(left, datetime.date) and isinstance(right, datetime.date):
        return float(left.toordinal()), float(right.toordinal())
    return None


def _str_order(a: str, b: str) -> tuple[float, float]:
    if a == b:
        return 0.0, 0.0
    return (-1.0, 0.0) if a < b else (1.0, 0.0)


def _criteria_match(value: Any, criteria: Any) -> bool:
    text = criteria if isinstance(criteria, str) else _display(criteria)
    text = text.strip()
    for prefix, op in (('>=', ast.GtE()), ('<=', ast.LtE()), ('<>', ast.NotEq()),
                       ('>', ast.Gt()), ('<', ast.Lt()), ('=', ast.Eq())):
        if text.startswith(prefix):
            operand = text[len(prefix):]
            try:
                return _compare(op, _as_number(value), float(operand.replace(',', '')))
            except (ValueError, FormulaError):
                return _compare(op, _display(value), operand)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) == float(text.replace(',', ''))
        except ValueError:
            pass
    if isinstance(value, bool):
        return _display(value).casefold() == text.casefold()
    if '*' in text or '?' in text:
        return fnmatch.fnmatchcase(_display(value).casefold(), text.casefold())
    return _display(value).casefold() == text.casefold()


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.date):
        return float(value.toordinal())
    if isinstance(value, str):
        return float(value.replace(',', ''))
    raise FormulaError(f'needs a number, not {value!r}')


def _rows(table: Any) -> list[list]:
    """A table argument as rows: ranges arrive nested, scalars arrive bare."""
    if isinstance(table, list):
        if table and all(isinstance(r, list) for r in table):
            return table
        return [table] if table and not any(isinstance(r, list) for r in table) else [
            r if isinstance(r, list) else [r] for r in table]
    return [[table]]


def _exact_match(key: Any, cell_value: Any) -> bool:
    if isinstance(key, bool) or isinstance(cell_value, bool):
        return key is cell_value
    if isinstance(key, (int, float)) and isinstance(cell_value, (int, float)):
        return float(key) == float(cell_value)
    return _display(key).casefold() == _display(cell_value).casefold()


def _eager_sum(args, formula):
    return sum(_nums(_flat(args)))


def _eager_average(args, formula):
    vals = _nums(_flat(args))
    if not vals:
        raise FormulaError(f'{formula!r}: AVERAGE of no numbers')
    return sum(vals) / len(vals)


def _eager_min(args, formula):
    vals = _nums(_flat(args))
    if not vals:
        raise FormulaError(f'{formula!r}: MIN of no numbers')
    return min(vals)


def _eager_max(args, formula):
    vals = _nums(_flat(args))
    if not vals:
        raise FormulaError(f'{formula!r}: MAX of no numbers')
    return max(vals)


def _eager_count(args, formula):
    return float(len(_nums(_flat(args))))


def _eager_counta(args, formula):
    return float(sum(1 for v in _flat(args) if v is not None and v != ''))


def _eager_round(args, formula):
    if not 1 <= len(args) <= 2:
        raise FormulaError(f'{formula!r} misuses ROUND')
    return round(_num(args[0], formula), int(_num(args[1], formula)) if len(args) == 2 else 0)


def _eager_abs(args, formula):
    if len(args) != 1:
        raise FormulaError(f'{formula!r} misuses ABS')
    return abs(_num(args[0], formula))


def _eager_concat(args, formula):
    return ''.join(_display(v) for v in _flat(args))


def _eager_len(args, formula):
    if len(args) != 1:
        raise FormulaError(f'{formula!r} misuses LEN')
    return len(_display(args[0]))


def _eager_upper(args, formula):
    if len(args) != 1:
        raise FormulaError(f'{formula!r} misuses UPPER')
    return _display(args[0]).upper()


def _eager_lower(args, formula):
    if len(args) != 1:
        raise FormulaError(f'{formula!r} misuses LOWER')
    return _display(args[0]).lower()


def _eager_trim(args, formula):
    if len(args) != 1:
        raise FormulaError(f'{formula!r} misuses TRIM')
    return re.sub(r'\s+', ' ', _display(args[0]).strip())


def _eager_today(args, formula):
    if args:
        raise FormulaError(f'{formula!r} misuses TODAY')
    return datetime.date.today()


def _eager_sumif(args, formula):
    if len(args) not in (2, 3):
        raise FormulaError(f'{formula!r} misuses SUMIF')
    total = 0.0
    for value, weight in _pair(args[0], args[2] if len(args) == 3 else args[0]):
        if _criteria_match(value, args[1]):
            total += _num(weight, formula) if weight not in (None, '') else 0.0
    return total


def _eager_countif(args, formula):
    if len(args) != 2:
        raise FormulaError(f'{formula!r} misuses COUNTIF')
    return float(sum(1 for v in _flat(args[0]) if _criteria_match(v, args[1])))


def _eager_averageif(args, formula):
    if len(args) not in (2, 3):
        raise FormulaError(f'{formula!r} misuses AVERAGEIF')
    vals = [_num(w, formula) if w not in (None, '') else 0.0
            for v, w in _pair(args[0], args[2] if len(args) == 3 else args[0])
            if _criteria_match(v, args[1])]
    if not vals:
        raise FormulaError(f'{formula!r}: AVERAGEIF of no numbers')
    return sum(vals) / len(vals)


def _pair(first: Any, second: Any) -> list[tuple[Any, Any]]:
    left = _flat(first)
    right = _flat(second)
    return list(zip(left, right))


def _eager_vlookup(args, formula):
    if len(args) not in (3, 4):
        raise FormulaError(f'{formula!r} misuses VLOOKUP')
    key, rows = args[0], _rows(args[1])
    try:
        col = int(_num(args[2], formula))
    except FormulaError as exc:
        raise FormulaError(f'{formula!r} misuses VLOOKUP') from exc
    if col < 1:
        raise FormulaError(f'{formula!r} misuses VLOOKUP')
    approx = _truthy(args[3]) if len(args) == 4 else False
    if not approx:
        for row in rows:
            if row and _exact_match(key, row[0]):
                try:
                    return row[col - 1]
                except IndexError as exc:
                    raise FormulaError(f'{formula!r}: column {col} is past the table') from exc
        raise FormulaError(f'{formula!r}: no match')
    best: Any = None
    found = False
    for row in rows:
        if not row:
            continue
        try:
            if _compare(ast.LtE(), row[0], key):
                best, found = row, True
        except FormulaError:
            continue
    if not found:
        raise FormulaError(f'{formula!r}: no match')
    try:
        return best[col - 1]
    except IndexError as exc:
        raise FormulaError(f'{formula!r}: column {col} is past the table') from exc


def _eager_xlookup(args, formula):
    if not 3 <= len(args) <= 6:
        raise FormulaError(f'{formula!r} misuses XLOOKUP')
    key, lookup, ret = args[0], _flat(args[1]), _flat(args[2])
    missing = args[3] if len(args) >= 4 else None
    match_mode = int(_num(args[4], formula)) if len(args) >= 5 else 0
    search_mode = int(_num(args[5], formula)) if len(args) == 6 else 1
    order = range(len(lookup)) if search_mode >= 0 else range(len(lookup) - 1, -1, -1)
    for i in order:
        candidate = lookup[i]
        if match_mode == 2 and isinstance(key, str):
            hit = fnmatch.fnmatchcase(_display(candidate).casefold(), key.casefold())
        elif match_mode in (0, -1, 1):
            hit = _exact_match(key, candidate)
        else:
            raise FormulaError(f'{formula!r}: match mode {match_mode} is not supported')
        if hit:
            try:
                return ret[i]
            except IndexError as exc:
                raise FormulaError(f'{formula!r}: return array is shorter than lookup') from exc
    if missing is not None:
        return missing
    raise FormulaError(f'{formula!r}: no match')


_EAGER = {
    'SUM': _eager_sum,
    'AVERAGE': _eager_average,
    'MIN': _eager_min,
    'MAX': _eager_max,
    'COUNT': _eager_count,
    'COUNTA': _eager_counta,
    'ROUND': _eager_round,
    'ABS': _eager_abs,
    'CONCAT': _eager_concat,
    'LEN': _eager_len,
    'UPPER': _eager_upper,
    'LOWER': _eager_lower,
    'TRIM': _eager_trim,
    'TODAY': _eager_today,
    'SUMIF': _eager_sumif,
    'COUNTIF': _eager_countif,
    'AVERAGEIF': _eager_averageif,
    'VLOOKUP': _eager_vlookup,
    'XLOOKUP': _eager_xlookup,
}


def json_value(value: Any) -> Any:
    """A cell value safe for JSON: dates as ISO text, anything exotic as text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def find_row(wb, sheet: str, match: dict[str, Any]) -> int | None:
    """The sheet row (1-based) whose header-named cells equal `match`."""
    ws = wb[sheet]
    headers = [str(c.value).strip() if c.value is not None else '' for c in ws[1]]
    try:
        cols = {k: headers.index(k) for k in match}
    except ValueError:
        return None
    for r, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(str(row[i].value).strip().lower() == str(match[k]).strip().lower()
               for k, i in cols.items()):
            return r
    return None


def column_letter(wb, sheet: str, header: str) -> str | None:
    from openpyxl.utils import get_column_letter

    for i, c in enumerate(wb[sheet][1], start=1):
        if c.value is not None and str(c.value).strip() == header:
            return get_column_letter(i)
    return None
