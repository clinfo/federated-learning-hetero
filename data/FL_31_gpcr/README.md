# GPCR compound–protein interaction dataset for federated learning

Compound-based federated-learning splits: every client holds a disjoint set of compounds,
while the protein space is shared across clients.

| Directory | Clients | Partition |
|---|---|---|
| `homo_3clients/` `homo_4clients/` `homo_5clients/` | 3 / 4 / 5 | homogeneous — clients hold comparable amounts of data |
| `hetero_3clients/` `hetero_4clients/` `hetero_5clients/` | 3 / 4 / 5 | heterogeneous — client sizes are strongly imbalanced |

Each directory holds one `clusterI.csv.gz` per client plus the configs used below.
`chembl_fl_gpcr_test.csv.gz` is the shared test set. Only the `hetero_*` directories carry
the fine-tuning configs.

## Running

Paths in the configs are relative to the repository root, so run from there. The example
below uses `hetero_3clients`; any other directory works the same way, with one client per
`clusterI.csv.gz`.

### Local baseline

Train and evaluate a single client on its own shard, with no federation:

```bash
kmol train data/FL_31_gpcr/hetero_3clients/graph_ligand+esm_protein_cluster1.json
kmol predict data/FL_31_gpcr/hetero_3clients/graph_ligand+esm_protein_cluster1_predict.json
```

Repeat for `cluster2` and `cluster3`.

### Federated learning

Start the server, then each client in its own shell:

```bash
mila server data/FL_31_gpcr/hetero_3clients/server.json
```

```bash
mila client data/FL_31_gpcr/hetero_3clients/client1.json
mila client data/FL_31_gpcr/hetero_3clients/client2.json
mila client data/FL_31_gpcr/hetero_3clients/client3.json
```

The server waits for all 3 clients, runs 10 rounds, and writes the aggregated model to
`hetero_3clients/server/10.aggregate.pt`. Evaluate it on the shared test set:

```bash
kmol predict data/FL_31_gpcr/hetero_3clients/graph_ligand+esm_protein_fl_predict.json
```

### Fine-tuning the federated model

Fine-tune the aggregated model on one client's shard, then evaluate it:

```bash
kmol train data/FL_31_gpcr/hetero_3clients/client1_finetuning.json
kmol predict data/FL_31_gpcr/hetero_3clients/client1_finetuning_predict.json
```

## Notes

Outputs (checkpoints, logs, aggregated models) are written into `server/`, `local/`,
`client/`, `clientI/` and `clusterI/` subdirectories here, which are git-ignored.

Each directory's `server.json` binds a fixed gRPC port, and ports are reused across
directories, so run one experiment at a time or edit `grpc_configuration.target`.
