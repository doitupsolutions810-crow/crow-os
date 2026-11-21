# Trademark Safety and Naming

Goal: no third party ever has a reason to pursue this project over a
name, and the project's own names are used consistently.

## Our names (use consistently, do not dilute)

Crow, Crow OS, Avrone, Great Scale, CycleKernal, CONTROL12, Crow
Sovereign Agent Platform. Keep one capitalization per name everywhere
(docs, READMEs, package names, UI text).

## Third-party marks (nominative use only)

| Mark | Owner | Where it appears | Rule |
|---|---|---|---|
| Grok | xAI | grok-bot-, control704-grokbot-knowledge | Factual reference to the API only. Do not use "Grok" in package names, product branding, logos or taglines. Prefer neutral names like "XAI connector" in user-facing surfaces. |
| Vercel | Vercel Inc. | scaffold repos, pinned apps | Reference only (links to deployments). Never imply endorsement. |
| GitHub / Actions | Microsoft | workflows, runner scripts | Nominative reference only. |
| Ollama | Ollama | llm.py, docs | Factual reference to the local server. |
| Trinity, Sovereign, Ghostshell | generic words, but used as titles elsewhere | trinity-core, runtime roles, ghostshell docs | Fine as internal codenames; do not use "Trinity" or "GhostShell" as the public product brand. Run a trademark search before any public brand push. |

## Standing rules

1. Third-party marks never appear in repository names for public
   products, in logos, or in marketing claims.
2. Where a third-party API is integrated, ship a THIRD_PARTY_NOTICES.md
   naming the owner and the nature of the integration, without
   endorsement claims.
3. Rename user-facing surfaces first; internal code references can
   follow later without risk.
4. Own names get a NOTICE line in crow-os: "(c) doitupsolutions810-crow;
   Crow OS and Avrone are product names of the owner."
