# Spike policy: does Trino ask OPA about procedures at all? Everything is allowed (file-based access control, chained
# before OPA, still decides who sees which catalog) except table procedures (ALTER TABLE ... EXECUTE) and catalog
# procedures (CALL) on catalog spike_opa. The decision log on stdout shows the input Trino sends for each check.
package trino

default allow := false

allow if not deny

deny if {
	input.action.operation == "ExecuteTableProcedure"
	input.action.resource.table.catalogName == "spike_opa"
}

deny if {
	input.action.operation == "ExecuteProcedure"
	input.action.resource.function.catalogName == "spike_opa"
}
