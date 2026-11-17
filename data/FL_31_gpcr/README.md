# GPCR compound–protein interaction dataset for federated learning

Compound-based federated-learning splits: every client holds a disjoint set of compounds,
while the protein space is shared across clients. See the repository README for how to run
these experiments.

| Directory | Clients | Partition |
|---|---|---|
| `homo_3clients/` `homo_4clients/` `homo_5clients/` | 3 / 4 / 5 | homogeneous — clients hold comparable amounts of data |
| `hetero_3clients/` `hetero_4clients/` `hetero_5clients/` | 3 / 4 / 5 | heterogeneous — client sizes are strongly imbalanced |

All six partitions cover the same 182,718 training records and differ only in how those
records are assigned to clients. `chembl_fl_gpcr_test.csv.gz` is the shared held-out test
set used by every prediction config.

Each directory contains, for `N` clients:

- `clusterI.csv.gz` — client `I`'s shard
- `clientI.json` / `server.json` — federated-learning communication configs
- `graph_ligand+esm_protein.json` — the global task config the server trains
- `graph_ligand+esm_protein_clusterI.json` — single-client (local-only) training config
- `graph_ligand+esm_protein_clusterI_predict.json` — evaluate client `I`'s local model
- `graph_ligand+esm_protein_fl_predict.json` — evaluate the aggregated federated model

The `hetero_*` directories additionally carry `clientI_finetuning.json` and
`clientI_finetuning_predict.json`, which fine-tune the aggregated federated model on a
single client's shard.

The training target is `t_1u`, a binary label for activity at a 1 µM threshold. The
remaining columns are metadata and are not consumed by the configs.
