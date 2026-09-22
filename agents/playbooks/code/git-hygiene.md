## Git hygiene (integrator only)

Commits, pushes and pull requests are the integrator's job — no other role
holds `git_commit`, `git_push` or `open_pull_request`.

- Commit only after the tests for the change have passed; the message says what
  and why in one line.
- Push the branch and open the PR only after a human has approved the exact
  diff. Never push past review.
- The PR body links the tasks and the test commands that passed. If anything
  failed, it says so rather than omitting it.
