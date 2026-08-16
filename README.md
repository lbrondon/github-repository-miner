# GitHub and GitLab Repository Miner

A Python application that validates a list of GitHub and GitLab URLs and
creates complete, concurrent, resumable, and observable Git clones. Selecting
only tracked `*.c` files is available as an optional mode.

This repository is the acquisition stage of a call-graph analysis pipeline.
Project analysis and result generation belong to a separate stage.

## Requirements

- Python 3.10 or later
- Git 2.25 or later available on `PATH`
- Network access to the listed repositories

The application uses only the Python standard library.

## Repository structure

~~~text
github-repository-miner/
|-- .repository-miner/
|   `-- mining-state.sqlite3
|-- input/
|   `-- 65_cs_repositories.txt
|-- output/
|   `-- projects/
|       `-- <repository>/
|-- source/
|   |-- main.py
|   |-- config.py
|   |-- exceptions.py
|   |-- repository.py
|   |-- mining_result.py
|   |-- ports.py
|   |-- repository_list_reader.py
|   |-- mining_service.py
|   |-- mining_state.py
|   `-- git_repository_cloner.py
|-- tests/
|-- pyproject.toml
`-- README.md
~~~

The `.repository-miner` directory and all cloned repositories are runtime
artifacts excluded from version control.

## Repository input

Add one GitHub or GitLab HTTPS URL per line. The default input is
`input/65_cs_repositories.txt`:

~~~text
https://github.com/curl/curl
https://gitlab.gnome.org/GNOME/libxml2
https://gitlab.com/group/subgroup/project.git
~~~

Blank lines, lines starting with `#`, and exact duplicates are ignored. By
default, invalid lines produce warnings while valid entries continue. Use
`--strict-input` to reject the complete file when any line is invalid.

Initial validation is syntactic. Repository existence and access permissions
can only be confirmed by Git. Entries with the same namespace and name on
different hosts are preserved and reported as possible mirrors.

Supported hosts are `github.com`, `gitlab.com`, and GitLab instances whose
host starts with `gitlab.`, such as `gitlab.gnome.org`. Nested GitLab groups
are supported. Web navigation paths such as `/-/tree/main` are rejected.

## Output layout

Every clone is a direct child of `output/projects`. A unique repository keeps
its original name. Repeated names receive a host suffix so every input entry
has a distinct destination without intermediate host directories:

~~~text
output/projects/curl/
output/projects/libxml2--github.com/
output/projects/libxml2--gitlab.gnome.org/
~~~

The default list produces exactly 65 direct child directories. Eight names are
disambiguated because the list contains four GitHub/GitLab mirror pairs.

### Previous layouts

Intermediate local versions used either
`projects/<host>/<namespace>/<repository>` or
`output/projects/<host>/<namespace>/<repository>`. The current application
does not move or delete those directories automatically. It reports a warning
instead, preventing silent changes to user data.

When a checkpoint destination differs from the current plan, that entry is
automatically returned to `pending` and cloned into the correct destination.

## Running the miner

Run from the repository root:

~~~bash
python3 source/main.py
~~~

Running from `source` is also supported:

~~~bash
cd source
python3 main.py
~~~

Relative command-line paths are always resolved from the repository root.
Running without arguments uses:

~~~text
input:       input/65_cs_repositories.txt
clones:      output/projects
checkpoint:  .repository-miner/mining-state.sqlite3
logging:     console only
clone mode:  complete shallow clone
~~~

### Recommended command for large repository lists

Start with four workers and adjust only after measuring network and disk
throughput:

~~~bash
python3 source/main.py \
  --repositories input/65_cs_repositories.txt \
  --projects-dir output/projects \
  --workers 4 \
  --clone-timeout 1800 \
  --max-retries 2 \
  --progress-interval 30
~~~

Use batches to limit the number of new clones started by one execution:

~~~bash
python3 source/main.py --batch-size 100
~~~

Run the same command again to select the next pending entries. Do not remove
completed clones during a campaign: checkpoint reconciliation returns every
missing completed destination to `pending` so that the final output remains
complete.

### Main options

| Option | Default | Purpose |
|---|---:|---|
| `--workers` | 4 | Maximum number of concurrent clones |
| `--clone-depth` | 1 | Number of commits in each shallow clone |
| `--clone-timeout` | 1800 | Timeout for each clone attempt, in seconds |
| `--max-retries` | 2 | Retries for transient failures |
| `--retry-base-delay` | 2 | Base exponential-backoff delay |
| `--progress-interval` | 30 | Interval between progress heartbeats |
| `--batch-size` | all | Maximum pending entries processed in one run |
| `--c-only` | disabled | Materialize only tracked `*.c` files |
| `--state-file` | `.repository-miner/mining-state.sqlite3` | SQLite checkpoint outside `output` |
| `--log-file` | disabled | Optional rotating file log; console remains active |
| `--strict-input` | disabled | Reject the input when any line is invalid |
| `--verbose` | disabled | Show clone starts and internal Git details |

Use `python3 source/main.py --help` for the complete option reference.

## Reliability and repeated execution

The service runs at most `--workers` Git processes at a time. Remaining items
stay in a lightweight in-memory queue. SQLite persists every repository as
`pending`, `running`, `succeeded`, or `failed`.

On every execution:

- a completed entry is skipped only while its directory and `.git` metadata
  still exist;
- a missing or invalid completed destination returns to `pending` and is
  cloned again;
- an interrupted `running` entry returns to `pending`;
- a clone finalized immediately before interruption is recovered as a success;
- failed entries are eligible for another attempt;
- a changed destination plan resets the affected checkpoint entry.

The filesystem and checkpoint are reconciled before work is selected. Removing
a project directory is therefore sufficient to schedule that project again.

Each clone starts in a unique partial directory. The destination becomes
visible only after Git succeeds and the partial directory is renamed
atomically. Normal failures and timeouts clean up the partial workspace.

The default clone command is equivalent to:

~~~text
git clone --depth 1 --single-branch --no-tags
~~~

The optional `--c-only` mode also uses `--filter=blob:none --no-checkout` and a
sparse checkout for `*.c`. Use a separate `--projects-dir` and `--state-file`
when switching modes because a completed checkpoint does not rematerialize an
existing working tree in another mode.

Every Git process receives:

~~~text
GIT_TERMINAL_PROMPT=0
GIT_LFS_SKIP_SMUDGE=1
~~~

Interactive authentication cannot silently block a worker. Large Git LFS
objects are not downloaded automatically; LFS files remain pointers unless a
later stage downloads them explicitly. Submodules are not cloned
automatically.

## Scalability

The scheduler keeps at most `workers` futures active rather than submitting
the complete input list to the executor. SQLite uses WAL mode and indexed
status queries. A local 5,000-entry control-plane benchmark with a no-I/O
cloner and 16 workers completed in approximately 1.3 seconds. Actual campaign
performance is dominated by:

1. Git transfer volume and remote latency;
2. filesystem write throughput and inode availability;
3. default-branch working-tree size;
4. network and disk contention caused by excessive workers;
5. clone retention and downstream processing time.

Four workers are the recommended starting point for one machine. Increases to
8 or 16 should be based on measured throughput, latency, disk usage, and error
rates.

## Reproducibility and transparency

Repeated executions in the same workspace are operationally idempotent. Valid
clones are reused, missing clones return to `pending`, and the 65 destination
names are deterministic. SQLite records status, attempt count, last error,
destination, and update time.

The current input locks URLs, not commits. A clean campaign on a later date may
therefore obtain newer default-branch revisions. A study that requires
bit-for-bit reproduction must record every clone's `HEAD` and preserve that
revision manifest with its results. Native revision-lockfile support is a
future improvement, not a guarantee of this version.

Logs go to the console by default. For a persistent operational trail without
adding files to `output`, use:

~~~bash
python3 source/main.py \
  --log-file .repository-miner/repository-miner.log
~~~

## Observability

Every log line includes a run identifier and thread name. Console output
reports:

- total, selected, completed, and pending repositories;
- active clones and queued entries;
- individual success or failure;
- completion percentage and elapsed time;
- throughput and estimated remaining time;
- the oldest active clone and its duration;
- free disk space on the destination volume;
- retries and their delays;
- checkpoint reconciliation results.

`--verbose` adds DEBUG events to the console. An explicitly configured log file
rotates at 10 MiB and keeps five backups.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | The selected batch finished without failures |
| 1 | One or more clones failed |
| 2 | Invalid configuration, input, Git, output, or checkpoint |
| 130 | Execution interrupted by the user |

When `--batch-size` leaves pending entries, the exit code remains 0 if the
selected batch completed without failures. The remaining count appears in the
console.

An existing final destination without a successful checkpoint is rejected to
prevent silent overwrites.

## Architecture

| Component | Responsibility |
|---|---|
| `source/main.py` | CLI, path resolution, logging, and service composition |
| `source/repository_list_reader.py` | UTF-8 input, independent line validation, and duplicate removal |
| `source/repository.py` | Validated identities and collision-free direct destination planning |
| `source/git_repository_cloner.py` | Shallow clone, optional sparse mode, timeout, atomicity, and error classification |
| `RetryingRepositoryCloner` | Exponential backoff with jitter for transient failures |
| `source/mining_service.py` | Bounded concurrent queue, result collection, and progress telemetry |
| `source/mining_state.py` | SQLite/WAL checkpoint and interruption recovery |

## Tests

The test suite does not access the network:

~~~bash
PYTHONPATH=source python3 -m unittest discover -s tests -v
~~~
