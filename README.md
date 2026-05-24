# TwitterSnipper Backup

Public sanitized backup of Kevin's X/Twitter discovery scripts.

## Contents

### `alpha_account_scout/`
Main crypto alpha account discovery workflow.

- Discovers early X/Twitter accounts/projects.
- Pushes accepted candidates to Main Notion.
- Pushes rejects to Reject Notion.
- Enriches with frontrun and assigns quality/tier when available.

Notion targets (IDs only, no tokens):

- Main Alpha DB: `99aaf8afdaf34eb79793d6b9518a922a`
- Reject DB: `33e878f48f9880a89558d548026ac91e`

### `nft_twitter_bot/`
NFT-specific recent-following monitor.

- No LLM dependency/cost.
- Uses `requests` only.
- Monitors CT/NFT seed accounts and routes:
  - NFT project / collection / onchain artist -> NFT Main Notion
  - CT but not NFT -> Reject Notion
  - generic/non-CT/noisy -> local reject only

Notion targets (IDs only, no tokens):

- NFT Early List DB: `ccb633a6-5089-4982-a158-c479864b72f9`
- Reject DB: `33e878f48f9880a89558d548026ac91e`

## Secrets intentionally excluded

This public repo must never include:

- X/Twitter `auth_token`, `ct0`, cookies, cookie pools
- Notion integration tokens (`ntn_...`)
- Frontrun session tokens
- API keys
- proxy credentials
- runtime state files / logs / caches

Use `.env.example` files as references only.
