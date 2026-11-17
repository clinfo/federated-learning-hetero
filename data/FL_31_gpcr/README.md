# GPCR compound–protein interaction dataset for federated learning

ChEMBL-derived GPCR compound–protein interaction data, partitioned across simulated
pharma clients. Splits are **compound-based**: every client holds a disjoint set of
compounds, while the protein space (563 GPCR targets) is shared across clients.

## Layout

| Directory | Clients | Partition |
|---|---|---|
| `homo_3clients/` `homo_4clients/` `homo_5clients/` | 3 / 4 / 5 | homogeneous — clients hold comparable amounts of data |
| `hetero_3clients/` `hetero_4clients/` `hetero_5clients/` | 3 / 4 / 5 | heterogeneous — client sizes are strongly imbalanced |

All six partitions cover the **same 182,718 training records**; they differ only in how
those records are assigned to clients. `chembl_fl_gpcr_test.csv.gz` is the shared held-out
test set used by every prediction config.

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

## Columns

The training target is `t_1u`, a binary label for activity at a 1 µM threshold. The
remaining columns are metadata and are not consumed by the configs.

## Running

Paths in the configs are relative to the repository root, so run from there:

```bash
# local (single-client) baseline
kmol train data/FL_31_gpcr/hetero_3clients/graph_ligand+esm_protein_cluster1.json

# federated learning: start the server, then each client in its own shell
mila server data/FL_31_gpcr/hetero_3clients/server.json
mila client data/FL_31_gpcr/hetero_3clients/client1.json

# evaluate the aggregated model on the shared test set
kmol predict data/FL_31_gpcr/hetero_3clients/graph_ligand+esm_protein_fl_predict.json
```

Outputs (checkpoints, logs, aggregated models) are written into `server/`, `local/`,
`client/`, `clientI/` and `clusterI/` subdirectories here, which are git-ignored.

Each directory's `server.json` binds a fixed gRPC port, and ports are reused across
directories, so run one experiment at a time or edit `grpc_configuration.target`.
