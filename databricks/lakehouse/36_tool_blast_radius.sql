-- Agent tool: walk the knowledge graph. Which GPUs depend on this entity?
CREATE OR REPLACE FUNCTION ${evidence}.tool_blast_radius(
  p_entity_id STRING COMMENT 'Any GPU, server, rack or cooling zone ID')
RETURNS TABLE
COMMENT 'Agent tool. GPUs that depend on an entity through the topology graph, and how (self, hosted_in, racked_in, cooled_by). Use it to turn one suspect component into the set of devices and jobs it could affect.'
RETURN
SELECT gpu_id, vendor, server_id, rack_id, cooling_zone_id,
       CASE WHEN gpu_id = p_entity_id THEN 'self'
            WHEN server_id = p_entity_id THEN 'hosted_in'
            WHEN rack_id = p_entity_id THEN 'racked_in'
            ELSE 'cooled_by' END AS dependency
FROM ${evidence}.inventory
WHERE p_entity_id IN (gpu_id, server_id, rack_id, cooling_zone_id)
ORDER BY gpu_id
LIMIT 24
