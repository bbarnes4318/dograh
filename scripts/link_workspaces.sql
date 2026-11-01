-- Link a workspace so another workspace's inbound routing can split calls to
-- its agents (see api/services/telephony/linked_workspaces.py).
--
-- After this, the source workspace's Inbound Routing page lists the linked
-- workspace's agents, labelled with :label, next to its own. A routing group
-- can then send e.g. 50% of its calls to a Final Expense agent of its own and
-- 50% to the linked workspace's Real Estate agent. Those calls are the linked
-- workspace's runs: they show up there. The numbers, the routing group and
-- the split stay with the source workspace.
--
-- One-way: the linked workspace gets nothing of the source's. Operator-only on
-- purpose (no API sets this): it hands one tenant's calls to another.
--
-- On ARI, the linked workspace needs an ARI telephony configuration on the
-- same Asterisk app as the source's numbers (clone_workspace.sql copies
-- them). Until it has one, its agents are skipped and the split's other
-- agents take the call; nothing is dropped.
--
-- Usage (on the box that runs the voice app; the container name may differ):
--
--   cat scripts/link_workspaces.sql | docker exec -i <postgres container> \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 \
--       -v source_org=1 -v linked_org=3 -v label='Real estate'
--
-- Re-running with the same pair updates the label. To unlink, add
-- -v unlink=1. One transaction.

\set QUIET on
\if :{?unlink}
\else
  \set unlink 0
\endif
BEGIN;

-- psql stops here (ON_ERROR_STOP) if either workspace doesn't exist.
SELECT id AS source_org_ok FROM organizations WHERE id = :source_org \gset
SELECT id AS linked_org_ok FROM organizations WHERE id = :linked_org \gset
SELECT (:source_org)::int <> (:linked_org)::int AS distinct_orgs \gset
\if :distinct_orgs
\else
  \echo 'source_org and linked_org are the same workspace; nothing changed.'
  ROLLBACK;
  \quit
\endif

INSERT INTO organization_configurations
  (organization_id, key, value, created_at, updated_at)
VALUES (:source_org, 'INBOUND_ROUTING_LINKED_WORKSPACES', '{"workspaces": []}'::json, now(), now())
ON CONFLICT ON CONSTRAINT _organization_key_uc DO NOTHING;

UPDATE organization_configurations c
   SET value = json_build_object(
         'workspaces',
         coalesce((
           SELECT jsonb_agg(e ORDER BY (e->>'organization_id')::int)
             FROM (
               SELECT e
                 FROM jsonb_array_elements(
                        coalesce(c.value::jsonb->'workspaces', '[]'::jsonb)) AS e
                WHERE jsonb_typeof(e) = 'object'
                  AND e->>'organization_id' IS DISTINCT FROM (:linked_org)::text
               UNION ALL
               SELECT jsonb_build_object('organization_id', (:linked_org)::int,
                                         'label', :'label')
                WHERE NOT (:unlink)::int::boolean
             ) AS kept
         ), '[]'::jsonb)
       )::json,
       updated_at = now()
 WHERE c.organization_id = :source_org
   AND c.key = 'INBOUND_ROUTING_LINKED_WORKSPACES';

\set QUIET off
SELECT value AS linked_workspaces
  FROM organization_configurations
 WHERE organization_id = :source_org AND key = 'INBOUND_ROUTING_LINKED_WORKSPACES';

SELECT id AS linked_ari_config, name, credentials::jsonb->>'app_name' AS ari_app
  FROM telephony_configurations
 WHERE organization_id = :linked_org AND provider = 'ari'
 ORDER BY id;
COMMIT;
