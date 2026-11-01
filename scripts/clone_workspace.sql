-- Clone a workspace's settings into a brand-new, separate workspace.
--
-- Creates one new user and one new organization, and copies the source
-- organization's SETTINGS into it -- never its agents. The new workspace
-- starts with no workflows, campaigns, runs, recordings, phone numbers, inbound
-- routing, folders, tools, knowledge base or API keys, so its agents are its
-- own and the two workspaces never see each other's.
--
-- Copied (as they are now; later edits to either side are independent):
--   organization_configurations   every key (AI models, voices, defaults, ...)
--                                 except INBOUND_ROUTING_LINKED_WORKSPACES
--   user_configurations           the source user's keys, onto the new user
--   telephony_configurations      carrier accounts, names, default outbound.
--                                 ARI configs keep the same Asterisk app as
--                                 the source; the API handles each call for
--                                 the workspace it belongs to (by its run or
--                                 called number), so sharing the app is safe
--   integrations                  connections and the calls they fire on
--   external_credentials          stored credentials (new ids)
--
-- Not copied, on purpose:
--   telephony_phone_numbers       a number can answer for one workspace only;
--                                 add the new workspace's own numbers in its UI
--   inbound_routing_*             routes point at the source's agents. To
--                                 split the source's calls to the new
--                                 workspace's agents, link it:
--                                 scripts/link_workspaces.sql
--   tools                         part of an agent; rebuild with the agents
--   api_keys                      secrets; issue new ones in the new workspace
--
-- Usage (on the box that runs the voice app; the container name may differ):
--
--   cat scripts/clone_workspace.sql | docker exec -i <postgres container> \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 \
--       -v source_org=1 -v source_user=1 \
--       -v provider_id=hoppwhistle-home-payouts \
--       -v email=voice@homepayouts.com
--
-- It prints the new user id and organization id. Give the USER id to
-- Hoppwhistle (`re-workspace voice --dograh-user <id>`), which signs the
-- workspace's people into this new account.
--
-- Safe to re-run: a second run with the same provider_id says so and changes
-- nothing. All-or-nothing: one transaction. Needs
-- PostgreSQL 13+ (gen_random_uuid).

\set QUIET on
BEGIN;

-- Checks. psql stops on the first that fails (ON_ERROR_STOP): \gset is an
-- error when its query returns no row, and users.provider_id is unique.
SELECT id AS source_org_ok FROM organizations WHERE id = :source_org \gset
SELECT id AS source_user_ok FROM users WHERE id = :source_user \gset
SELECT NOT EXISTS (SELECT 1 FROM users WHERE provider_id = :'provider_id') AS fresh \gset
\if :fresh
\else
  \echo 'A workspace with this provider_id already exists; nothing changed.'
  ROLLBACK;
  \quit
\endif

INSERT INTO users (provider_id, email, is_superuser, created_at)
VALUES (:'provider_id', lower(:'email'), false, now())
RETURNING id AS new_user_id \gset

INSERT INTO organizations (
  provider_id, created_at, quota_type, quota_dograh_tokens, quota_reset_day,
  quota_start_date, quota_enabled, price_per_second_usd
)
SELECT 'org_' || :'provider_id', now(), quota_type, quota_dograh_tokens, quota_reset_day,
       quota_start_date, quota_enabled, price_per_second_usd
  FROM organizations WHERE id = :source_org
RETURNING id AS new_org_id \gset

INSERT INTO organization_users (user_id, organization_id) VALUES (:new_user_id, :new_org_id);
UPDATE users SET selected_organization_id = :new_org_id WHERE id = :new_user_id;

INSERT INTO organization_configurations
  (organization_id, key, value, created_at, updated_at, last_validated_at)
SELECT :new_org_id, key, value, now(), now(), last_validated_at
  FROM organization_configurations
 WHERE organization_id = :source_org
   AND key <> 'INBOUND_ROUTING_LINKED_WORKSPACES';

INSERT INTO user_configurations (user_id, key, configuration, last_validated_at)
SELECT :new_user_id, key, configuration, last_validated_at
  FROM user_configurations WHERE user_id = :source_user;

INSERT INTO telephony_configurations
  (organization_id, name, provider, credentials, is_default_outbound, created_at, updated_at)
SELECT :new_org_id, name, provider, credentials, is_default_outbound, now(), now()
  FROM telephony_configurations WHERE organization_id = :source_org;

INSERT INTO integrations
  (integration_id, organization_id, provider, created_by, is_active, created_at,
   connection_details, action)
SELECT integration_id, :new_org_id, provider, :new_user_id, is_active, now(),
       connection_details, action
  FROM integrations WHERE organization_id = :source_org;

INSERT INTO external_credentials
  (credential_uuid, organization_id, name, description, credential_type, credential_data,
   created_by, created_at, updated_at, is_active)
SELECT gen_random_uuid()::text, :new_org_id, name, description, credential_type,
       credential_data, :new_user_id, now(), now(), is_active
  FROM external_credentials WHERE organization_id = :source_org;

COMMIT;
\set QUIET off

SELECT :new_user_id AS new_user_id, :new_org_id AS new_organization_id,
       (SELECT count(*) FROM organization_configurations WHERE organization_id = :new_org_id) AS settings,
       (SELECT count(*) FROM telephony_configurations WHERE organization_id = :new_org_id) AS telephony,
       (SELECT count(*) FROM integrations WHERE organization_id = :new_org_id) AS integrations,
       (SELECT count(*) FROM external_credentials WHERE organization_id = :new_org_id) AS credentials;
