# Security policy

## Reporting a vulnerability

Report a suspected vulnerability in alrust privately, through GitHub's
private vulnerability reporting: open the repository's **Security** tab and
choose **Report a vulnerability**. Do not open a public issue, pull request or
discussion for it, and do not include real credentials, customer data or
private logs; synthetic reproductions are enough.

A useful report names the source revision (a commit SHA), the platform, the
affected route or component, the steps that reproduce it, and the impact you
expect.

## Scope

- In scope: the code in this repository, its build files and its workflows.
- Out of scope here: Alerta itself and its web UI. alrust pins them read-only
  under `reference/`; report their vulnerabilities to the Alerta project.

## Supported versions

alrust is an early alpha with no releases yet. Fixes land on `main` only.
