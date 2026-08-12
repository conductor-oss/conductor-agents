# Workflow versioning policy

This harness maintains exactly one registered version of each workflow: **version 1**.

- Every workflow definition must declare `"version": 1`.
- Every `SUB_WORKFLOW` reference must pin its child to version `1`.
- Deploy changes with `conductor workflow update` against version 1; do not create a new workflow version.
- After deployment, remove every registered version other than version 1, after checking there is no active execution that must remain on the obsolete definition.

`workers/register.sh` is the canonical deployment path. It updates an existing workflow definition in place and creates it only when it does not exist.
