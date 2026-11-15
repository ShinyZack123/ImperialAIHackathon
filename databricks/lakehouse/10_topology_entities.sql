-- Knowledge-graph nodes derived from inventory. A view, so the graph can never drift from the source.
CREATE OR REPLACE VIEW ${evidence}.topology_entities
COMMENT 'Graph nodes for the datacenter knowledge graph: one row per GPU, server, rack and cooling zone. Derived from inventory.'
AS
SELECT gpu_id AS entity_id, 'gpu' AS entity_type, server_id AS parent_entity_id, vendor, model FROM ${evidence}.inventory
UNION ALL
SELECT DISTINCT server_id, 'server', rack_id, CAST(NULL AS STRING), CAST(NULL AS STRING) FROM ${evidence}.inventory
UNION ALL
SELECT DISTINCT rack_id, 'rack', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING) FROM ${evidence}.inventory
UNION ALL
SELECT DISTINCT cooling_zone_id, 'cooling_system', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING) FROM ${evidence}.inventory
