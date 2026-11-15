-- Knowledge-graph edges. Direction is "source depends on destination".
CREATE OR REPLACE VIEW ${evidence}.topology_connections
COMMENT 'Graph edges for the datacenter knowledge graph. source_entity_id depends on destination_entity_id through the named relationship.'
AS
SELECT gpu_id AS source_entity_id, server_id AS destination_entity_id, 'hosted_in' AS relationship FROM ${evidence}.inventory
UNION ALL
SELECT DISTINCT server_id, rack_id, 'racked_in' FROM ${evidence}.inventory
UNION ALL
SELECT gpu_id, cooling_zone_id, 'cooled_by' FROM ${evidence}.inventory
