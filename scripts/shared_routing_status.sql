-- Read-only status report for shared numbers and cross-workspace inbound
-- routing (api/services/telephony/shared_numbers.py, linked_workspaces.py).
-- Changes nothing: one READ ONLY transaction, rolled back at the end.
--
-- Use it before activating a split (to find the real workspace and agent ids)
-- and after (to confirm what the routing will do).
--
--   cat scripts/shared_routing_status.sql | docker exec -i <postgres container> \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 -v source_org=1
--
-- source_org is the workspace that owns the numbers (the insurance / Final
-- Expense workspace). Prints no carrier credentials: only each ARI config's
-- app name, which must match across the workspaces for ARI routing.

\set QUIET on
BEGIN TRANSACTION READ ONLY;
\set QUIET off

\echo '== 1. Workspaces (org id, first user, ARI configs and their Asterisk app)'
SELECT o.id AS org_id,
       (SELECT u.email FROM organization_users ou JOIN users u ON u.id = ou.user_id
         WHERE ou.organization_id = o.id ORDER BY u.id LIMIT 1) AS first_user,
       string_agg(DISTINCT tc.id || ':' || tc.name || ' app=' ||
                  coalesce(tc.credentials::jsonb->>'app_name', '?'), ', ') AS ari_configs
  FROM organizations o
  LEFT JOIN telephony_configurations tc
         ON tc.organization_id = o.id AND tc.provider = 'ari'
 GROUP BY o.id ORDER BY o.id;

\echo '== 2. Link setting on the source workspace (operator-set)'
SELECT organization_id, value
  FROM organization_configurations
 WHERE key = 'INBOUND_ROUTING_LINKED_WORKSPACES'
 ORDER BY organization_id;

\echo '== 3. Linked workspaces with NO ARI config on the source''s Asterisk app'
\echo '   (their agents would be skipped on ARI; empty = OK)'
WITH link AS (
  SELECT (e->>'organization_id')::int AS org_id, e->>'label' AS label
    FROM organization_configurations c,
         jsonb_array_elements(coalesce(c.value::jsonb->'workspaces', '[]')) e
   WHERE c.organization_id = :source_org
     AND c.key = 'INBOUND_ROUTING_LINKED_WORKSPACES'
), source_apps AS (
  SELECT DISTINCT credentials::jsonb->>'app_name' AS app
    FROM telephony_configurations
   WHERE organization_id = :source_org AND provider = 'ari'
)
SELECT link.org_id, link.label
  FROM link
 WHERE NOT EXISTS (
   SELECT 1 FROM telephony_configurations tc
    WHERE tc.organization_id = link.org_id AND tc.provider = 'ari'
      AND tc.credentials::jsonb->>'app_name' IN (SELECT app FROM source_apps));

\echo '== 4. Active agents in the source and linked workspaces (use these workflow ids)'
WITH orgs AS (
  SELECT :source_org::int AS org_id
  UNION
  SELECT (e->>'organization_id')::int
    FROM organization_configurations c,
         jsonb_array_elements(coalesce(c.value::jsonb->'workspaces', '[]')) e
   WHERE c.organization_id = :source_org
     AND c.key = 'INBOUND_ROUTING_LINKED_WORKSPACES'
)
SELECT w.organization_id AS org_id, w.id AS workflow_id, w.name
  FROM workflows w JOIN orgs ON orgs.org_id = w.organization_id
 WHERE w.status = 'active'
 ORDER BY w.organization_id, w.name;

\echo '== 5. Routing groups of the source workspace and their split'
SELECT g.id AS group_id, g.name, g.is_active, g.sticky_by_caller,
       string_agg(t.workflow_id || ' ' || coalesce(w.name, '?') || ' (org ' ||
                  coalesce(w.organization_id::text, '?') || ') ' || t.weight || '%',
                  ' / ' ORDER BY t.weight DESC, t.workflow_id) AS split,
       sum(CASE WHEN t.weight > 0 THEN t.weight ELSE 0 END) AS total_weight,
       (SELECT count(*) FROM telephony_phone_numbers p
         WHERE p.inbound_routing_group_id = g.id) AS numbers
  FROM inbound_routing_groups g
  LEFT JOIN inbound_routing_targets t ON t.group_id = g.id
  LEFT JOIN workflows w ON w.id = t.workflow_id
 WHERE g.organization_id = :source_org
 GROUP BY g.id ORDER BY g.id;

\echo '== 6. Source numbers by configuration: in a group / own agent only / unrouted'
SELECT tc.id AS config_id, tc.name, tc.provider,
       count(*) FILTER (WHERE p.is_active) AS active,
       count(*) FILTER (WHERE p.inbound_routing_group_id IS NOT NULL) AS in_a_group,
       count(*) FILTER (WHERE p.inbound_routing_group_id IS NULL
                          AND p.inbound_workflow_id IS NOT NULL) AS own_agent_only,
       count(*) FILTER (WHERE p.inbound_routing_group_id IS NULL
                          AND p.inbound_workflow_id IS NULL) AS unrouted
  FROM telephony_configurations tc
  JOIN telephony_phone_numbers p ON p.telephony_configuration_id = tc.id
 WHERE tc.organization_id = :source_org
 GROUP BY tc.id ORDER BY tc.id;

\echo '== 7. The same DID routed in more than one workspace (owner wins; clean these up)'
SELECT p.address_normalized, string_agg(p.organization_id::text || ':' ||
       coalesce(p.inbound_routing_group_id::text, 'agent ' || p.inbound_workflow_id), ', ') AS routed_in
  FROM telephony_phone_numbers p
 WHERE p.is_active
   AND (p.inbound_workflow_id IS NOT NULL OR p.inbound_routing_group_id IS NOT NULL)
 GROUP BY p.address_normalized
HAVING count(DISTINCT p.organization_id) > 1
 ORDER BY 1 LIMIT 50;

\echo '== 8. Last 24h: calls each group sent to each workspace'
SELECT r.initial_context->'inbound_routing'->>'group_id' AS group_id,
       w.organization_id AS ran_in_org, w.id AS workflow_id, w.name, count(*) AS calls,
       count(*) FILTER (WHERE (r.initial_context->'inbound_routing'->>'changed_workspace')::boolean)
         AS fell_back_across_workspaces
  FROM workflow_runs r JOIN workflows w ON w.id = r.workflow_id
 WHERE r.created_at > now() - interval '24 hours'
   AND r.initial_context->'inbound_routing' IS NOT NULL
 GROUP BY 1, 2, 3, 4 ORDER BY 1, calls DESC;

ROLLBACK;
