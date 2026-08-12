# Test C — Clean Planned Switchover (db2 → db1)

Method: `patronictl switchover --leader db2 --candidate db1 --force` while a continuous workload
writes through the VIP. Validates the `on_role_change` → `pcp_promote_node` callback (playbook 03
bakes the signal in; this closes the write gap vs. the pre-fix behavior of ~4 minutes).

## Timeline — T0 2026-08-12 05:06:51Z

| Time (Z) | Event |
|---|---|
| 05:06:51 | switchover initiated |
| 05:06:57 | last pre-switchover CONFIRMED |
| 05:07:02 | single FAILED event (in-flight id 8462) |
| 05:07:05 | CONFIRMED 8462 (retried), writes resume |
| shortly after | db1 Leader TL 6; db2+db3 streaming, 0 lag |

**VIP write blip ≈ 8 seconds (single-digit). 0 lost commits.**

