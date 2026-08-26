# Present so pytest treats this directory as the rootdir and puts it on
# sys.path; without it the apps cannot import each other. It is otherwise
# deliberately empty — the `collect_ignore` entry it used to carry existed only
# because `executor/test_generator.py` matched pytest's `test_*` collection
# pattern. That module was renamed to `executor/sample_inputs.py` and then
# deleted with the workflow "test run" feature it existed to serve, so the
# exemption is gone rather than being carried forward.
