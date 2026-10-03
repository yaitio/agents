# Deploying

From a fork of this repository to a running service in your own AWS account.

The rule behind every step: **as little as possible is mandatory, nothing secret
is ever in the repository, and the defaults are safe.**

## What you need

| | Why |
|---|---|
| An AWS account and a region | where the service runs and where its data lives |
| Credentials that may manage IAM, once | three setup steps the deploy is deliberately unable to do for itself |
| A GitHub fork of this repository | the release runs in your fork's Actions |
| Python 3.12 on your machine | the setup scripts, and the installation's signing key, which never leaves it |

**No model key.** Each caller brings their own provider key with every request
and pays their provider directly. The installation pays AWS for infrastructure,
never for tokens, and stores no provider key anywhere.

## 1. Prepare the AWS account

Set a budget alert in **AWS Budgets** before anything else.

## 2. Set up, with credentials that may manage IAM

Two things the deploy may not do for itself. `python3 scripts/names.py` prints
every name in use; the commands below use the defaults and `us-west-1`.

```bash
REGION=us-west-1
# The templates carry "Comment" fields for the reader; IAM refuses them.
fill() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d.pop('Comment',None); [s.pop('Comment',None) for s in d['Statement']]; print(json.dumps(d, indent=1).replace('REGION','$REGION').replace('DATA_POLICY','yait_agents_data').replace('PREFIX','yait_agents'))" "$1"; }

# a. the deploy user, and its key
fill infra/deploy-policy.json > /tmp/deploy-policy.json
aws iam create-user --user-name yait-agents-deploy
aws iam put-user-policy --user-name yait-agents-deploy \
  --policy-name deploy --policy-document file:///tmp/deploy-policy.json
aws iam create-access-key --user-name yait-agents-deploy

# b. the policy both functions run with — the deploy may attach it, never write it
fill infra/function-policy.json > /tmp/function-policy.json
aws iam create-policy --policy-name yait_agents_data \
  --policy-document file:///tmp/function-policy.json
```

If you rename anything (step 4), generate the deploy policy with
`python3 scripts/names.py --policy --region $REGION` instead: it names the
functions, the role path and the tables, and a policy that names the old ones is
an `AccessDenied` halfway through a release.

**Why (b) is yours and not the deploy's.** A release that can write a policy,
attach it to a role it created and pass that role to Lambda can run anything as
an administrator. So the deploy may attach exactly two policies — AWS's logs
policy and this one, enforced by an `iam:PolicyARN` condition — and can create
neither. The cost is one command at setup.

### Which rights sit on which identity

| Identity | Created by | Holds | Notably does **not** hold |
|---|---|---|---|
| **You**, at setup | — | IAM and Secrets Manager in the account | — use it for setup and rotation, never in a pipeline |
| **Deploy user** — step 2a, key in your fork's secrets | you | [`infra/deploy-policy.json`](../infra/deploy-policy.json): the two functions, roles under one path, the three tables, API Gateway, and attaching exactly two policies | `iam:PutRolePolicy`, `CreatePolicy`, `CreateUser`, any secret — denied explicitly. It can neither widen itself nor write a credential |
| **The functions' roles** — made by the deploy | the deploy; **you** made their policy | [`infra/function-policy.json`](../infra/function-policy.json): logs, the three tables, and reading the vault key | anything else in the account |

## 3. Put the credentials in your fork

**Settings → Secrets and variables → Actions.** Which tab matters:

| | Tab |
|---|---|
| `AWS_ACCESS_KEY_ID` | **Secrets** |
| `AWS_SECRET_ACCESS_KEY` | **Secrets** |
| `AWS_REGION` | **Variables** — not a secret, and a secret would only make the logs unreadable |
| `YAIT_CLIENT_KEY` | **Secrets**, after step 6 — a token the release tests the deployment with. The first release runs without it |

From a terminal, which keeps the key out of a browser and your shell history:

```bash
gh secret set AWS_ACCESS_KEY_ID
gh secret set AWS_SECRET_ACCESS_KEY
gh variable set AWS_REGION --body us-west-1
```

Repository secrets, not environment secrets: the workflow declares no
`environment:`. Use an environment when you want a required reviewer before a
release, secrets unavailable from other branches, or more than one account —
then add `environment: <name>` to the job, or the secrets are invisible to it.

## 4. Settings (optional)

Every setting is an Actions **variable** with a default; set none and the
defaults apply.

| Variable | Default | |
|---|---|---|
| `YAIT_PREFIX` | `yait_agents` | moves every name below at once |
| `YAIT_FUNCTION_NAME`, `YAIT_WORKER_FUNCTION` | `yait_agents_api_functions`, `yait_agents_worker` | the two functions |
| `YAIT_ROLE_NAME`, `YAIT_WORKER_ROLE_NAME`, `YAIT_ROLE_PATH` | `yait_agents_api_role`, `yait_agents_worker_role`, `/yait_agents/` | their roles, and the path role creation is scoped to |
| `YAIT_API_NAME`, `STAGE_NAME` | `yait_agents_api`, `prod` | the REST API, and the first path segment of every URL |
| `YAIT_RUNTIME`, `YAIT_ARCHITECTURE` | `python3.12`, `arm64` | |
| `YAIT_MEMORY_MB`, `YAIT_TIMEOUT_S` | `256`, `10` | the API function: small, since it answers and returns |
| `YAIT_WORKER_MEMORY_MB` | `1024` | the step worker; memory buys its cold-start CPU — see [economics.md](economics.md) |
| `YAIT_ENDPOINTS` | OpenRouter only | named model servers, one per line: `mygpu=https://gpu.example.com/v1`. A caller asks for `mygpu/<model>` and brings that server's key. A line may end with a JSON object of request fields — `openrouter-fp8=https://openrouter.ai/api/v1 {"provider": {"quantizations": ["fp8"]}}`. `https://` only; checked before the release changes anything |
| `YAIT_MAX_TOKENS` | `32768` | the most one answer may take, thinking included |

**API Gateway takes the API's name from the imported document**, on the first
import and on every overwrite, so `YAIT_API_NAME` is written into that document
rather than applied by renaming the API afterwards — a rename by hand is undone
by the next release, which then fails to recognise its own API.

## 5. Deploy

Push to `main`, or run the **Deploy** workflow. It:

1. runs every test suite locally — contract, behaviour, worker, vault, tools —
   before touching AWS;
2. **checks the credentials carry every permission it will need**, and names any
   that is missing before a single resource is made;
3. prints the plan;
4. applies, prints the **service URL**, and — once `YAIT_CLIENT_KEY` exists —
   runs the contract and behaviour suites against the deployment.

The permission check is worth running by hand once:
`python3 scripts/check_permissions.py --region us-west-1`. It tells **absent**
(a real call was refused) from **doubtful** (IAM's simulation said no, and it
cannot see resource-level grants — settle those with `--probe-writes`, which
makes and deletes a throwaway API).

## 6. The installation's keys

On your machine, with the credentials of step 2, after the first release (it
makes the table the signing key goes in):

```bash
python3 scripts/keys.py keygen --region us-west-1      # the signing key pair
python3 scripts/keys.py vault-key --region us-west-1   # the key vault secrets are sealed with
python3 scripts/keys.py issue --tenant default --name ci   # → the fork's secret YAIT_CLIENT_KEY
```

- **`keygen`** keeps the private key in `~/.config/yait/<prefix>/`, readable by you
  alone, and puts the public key in the agents table. The service can check a
  token but never make one. Until a public key exists, every request is refused.
- **`vault-key`** makes `yait_agents/vault-key` in Secrets Manager, once.
  Replacing it would make every stored credential unreadable, so it refuses to.
- **`issue`** signs a token for one user. Its `--tenant` decides whose agents and
  sessions are whose; tokens do not expire. To cut off every token a key signed,
  `keys.py revoke-key --kid <kid> --region <region>` — within five minutes.

## 7. Use it

Send the token as `X-Yait-Key`; your provider key goes where it always does.

```python
from anthropic import Anthropic

client = Anthropic(base_url="https://<service-url>/anthropic",
                   api_key="<your Anthropic key>",
                   default_headers={"X-Yait-Key": "<token>"})
agent = client.beta.agents.create(model="claude-haiku-4-5-20251001", name="hello")
```

The OpenAI dialect is at `https://<service-url>/openai/v1`, with the provider key
as `Authorization: Bearer` and the token as `X-Yait-Key`. Any model takes the
provider key of the provider that serves it: `deepseek-flash` a DeepSeek key,
`openrouter/z-ai/glm-5.3-flash` an OpenRouter key.

## What ends up in your account

| | Default name | |
|---|---|---|
| API function | `yait_agents_api_functions` | 256 MB, 10 s; also API Gateway's authorizer |
| Step worker | `yait_agents_worker` | 1 GB, 900 s; invoked by the API function |
| Their roles | `yait_agents_api_role`, `yait_agents_worker_role` | on path `/yait_agents/` |
| REST API and stage | `yait_agents_api`, `prod` | with a request authorizer on `X-Yait-Key` |
| Tables | `yait_agents_sessions`, `_events`, `_agents` | on-demand DynamoDB |
| Log groups | `/aws/lambda/<function>` | made by Lambda, **with no expiry** — set a retention by hand in a long-lived account |
| Made by you | the deploy user, `yait_agents_data`, `yait_agents/vault-key`, the public key item in the agents table | |

**What holds a secret, and who reads it:**

| | Written by | Read by |
|---|---|---|
| the signing key's public half — an item in the agents table | you, `keygen` | the API, to check tokens |
| the signing key's private half | you, `keygen` | nobody else: it stays on your machine |
| `yait_agents/vault-key` | you, `vault-key` | both functions: the API seals a credential, the worker opens it for one MCP call |
| MCP credentials, sealed, in the agents table | callers, through the vaults API | the worker, per call; never returned by any API |
| provider keys | — | nowhere: they arrive with a request and are gone when it ends |

## Removing it

```bash
python3 scripts/deploy.py --region us-west-1 --delete
```

removes the functions, their roles and the API. **The tables stay**: they hold
the event log, the only record of what happened, and a release that can erase it
by accident is one nobody should run. Delete them by hand if you mean to, and the
policy, the deploy user and the vault key with them.

## Pitfalls

- **A public fork's Actions logs are public.** The workflows never print a key;
  keep it that way in anything you add.
- **The region is chosen once.** Sessions, agents and credentials live there;
  moving is a migration, not a setting.
- **Your own domain** is not automated yet: put an API Gateway custom domain or
  CloudFront in front of the stage by hand.
- **From a shell instead of Actions**, `python3 scripts/deploy.py --region …`
  with the deploy user's key works — it is the same script — but the release path
  is the product, and a deploy that only happens on one machine has not
  exercised it.
