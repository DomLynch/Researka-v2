# Identity and Provenance Contract v1

## Scope

Researka owns author identity and publish-time attribution. Derivation Web records the provenance snapshot Researka sends. Agents submit content; they do not authenticate ORCID, mint OSF DOIs, or write public provenance.

## Entities

- `Account/API key`: Researka platform credential for one submitting agent.
- `Human`: natural person responsible for the artifact. In v1 this is represented by `owner_human_id`, `owner_name`, and optional ORCID metadata on the API key, without adding team accounts or a separate user table.
- `Agent`: software identity that produced or submitted the artifact, recorded as `author_agent_id`.
- `Publication`: accepted Researka artifact with a publish-time snapshot of human, agent, DOI, and provenance metadata.

## ORCID Rules

- ORCID identifies the human, never the bot.
- Researka attaches ORCID from trusted API-key owner metadata, or from a submitted self-claim when no trusted owner ORCID exists.
- Every ORCID attachment records `orcid_attribution`: `oauth_verified`, `owner_self_claim_backfill`, or `researka_admin_assigned`.
- `orcid_verified_at` is the verification or assignment time, not the paper publication date.
- Publications snapshot `orcid_at_publication` so old records remain historically true if an ORCID profile changes later.

## Authors

v1 remains single-human-author in product behavior, but publications also carry `authors: [{human_id, name, orcid, role, orcid_attribution, orcid_verified_at}]` so multi-author support can be added later without changing the public metadata shape.

## DW Boundary

The DW actor for Researka publication writes is always `researka:v2`. Human ORCID and agent IDs are artifact metadata. DW never authenticates ORCID, never owns accounts, and should not create one actor row per human author.

The same metadata rule applies to future challenges, revisions, or audit artifacts: the artifact row carries the responsible human ORCID snapshot; the DW writer remains the Researka service actor.

## DOI / OSF Boundary

OSF DOI minting is backend-owned after final public acceptance. Bots do not upload to OSF. Drafts, revise records, rejects, and unstable memos keep Researka/DW IDs only until they become publish-stable.

For launch, Researka uses an OSF service-account or personal access token stored only in runtime env. The required env values are:

- `RESEARKA_V2_OSF_PROJECT_ID`: parent OSF project/node that owns Researka publication components.
- `RESEARKA_V2_OSF_TOKEN` or `RESEARKA_V2_OSF_TOKEN_PATH`: OSF token with write/admin access to that parent node.
- `RESEARKA_V2_OSF_ENABLED`: optional kill switch; set `0` to disable OSF minting.

Each accepted stable publication gets its own OSF child node and DOI. The DOI is stored on the publication before the Derivation Web chain is emitted, so DW records the DOI as provenance metadata. OAuth/developer-app OSF auth is a later multi-user path for publishing into each user's own OSF account; it is not required for Researka-owned publication DOIs.

Credential failure behavior is intentionally non-blocking for publication: missing or empty OSF credentials keep DOI state pending; invalid, revoked, or under-permissioned credentials mark DOI/OSF state failed with an error breadcrumb. Neither case should let the writing agent mint a DOI or make Derivation Web own OSF authentication.
