## Run tests before claiming done

A change is not done until the relevant tests have run.

- Run the smallest test command that covers your change (`test` class first).
- Report the exact command, whether it passed, and paste the tail of failures.
- If you cannot run tests, say so plainly and mark the task blocked — do not
  claim tests pass from reading the code.
- New behaviour needs a new test or an extended one; a fix without a regression
  test is a fix that will regress.
