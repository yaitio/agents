# Security

## Reporting a vulnerability

Report it privately, through **Security → Report a vulnerability** on this
repository's GitHub page. Please do not open a public issue, and do not test
against a deployment that is not yours.

Say what is affected, how to reproduce it, and what an attacker gains. We answer
within a week, and say when a fix is released and who found it, unless you would
rather not be named.

## What is in scope

This repository: the service in `src/`, the deploy and key scripts in `scripts/`,
the IAM policies in `infra/` and the release workflow. In particular:

- a request reaching a function without a valid token, or one tenant reaching
  another's agents, sessions or vaults;
- a vault credential readable through any API, log or event;
- a caller's provider key, a token or a credential outliving the invocation that
  carried it, or appearing in a log;
- the deploy user or the functions' roles doing more than
  [deploy.md](docs/deploy.md#which-rights-sit-on-which-identity) says.

Out of scope: the model providers, the MCP servers an agent is pointed at,
`aichain` (report those to its repository), and what an agent does with a tool it
was legitimately given.

## Running it safely

A deployment is yours to secure; [deploy.md](docs/deploy.md) and
[operations.md](docs/operations.md) cover the parts that are:

- keep the signing key's private half on the machine that made it;
- never replace the vault key — rotate it, keeping the old one;
- set a retention on the log groups, and a budget alert on the account;
- in a public fork, Actions logs are public: keep keys out of anything you add.
