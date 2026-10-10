---
name: cassandra-operator
description: Diagnose and recover a Cassandra node that fails to rejoin the cluster.
systems: ["cassandra", "k8ssandra"]
---

When a node won't rejoin, check `nodetool status` for a down node and follow the
runbook at docs/runbooks/cassandra-operations.md before running any destructive
`nodetool removenode` command.
