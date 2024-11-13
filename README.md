# jsi

[![PyPI - Version](https://img.shields.io/pypi/v/just-solve-it)](https://pypi.org/project/just-solve-it)

just solve it - a command-line utility to run a portfolio of [SMT](https://en.wikipedia.org/wiki/Satisfiability_modulo_theories) solvers in parallel

![Screenshot of jsi running on an unsat division problem](static/images/unsat-div-screenshot.png)


## Highlights

- 🏆 acts as a "virtual best solver" by running multiple solvers in parallel and printing the result of the fastest solver to stdout
- 🔍 discovers available solvers on the PATH at runtime
- 🛣️ runs solvers in parallel and monitors their progress
- 📜 parses solver output to determine if the problem is sat, unsat, error, unknown, etc
- ⏰ can terminate solvers after a timeout
- ⏸️ can be interrupted with Ctrl-C and remaining solvers will be killed
- 🏎 runs with minimal startup time (<100ms), and also supports an experimental daemon mode with a rust client for extra low-latency (<10ms)
- 🔪 reaps orphaned solver processes
- 🖥️ supports macOS and Linux
- 🐍 supports Python 3.11+


## Getting Started

We recommend using [uv](https://docs.astral.sh/uv/) to install jsi.

```sh
# install jsi
uv tool install just-solve-it

# run it
jsi --help
```

Verify the installation with one of the included examples:

```sh
jsi --full-run --timeout 2s examples/unsat-div.smt2
```


## Features

<details>
<summary>🧰 Configuration</summary>

This is how jsi finds and runs solvers:

- it first attempts to load custom solver definitions from `~/.jsi/solvers.json`
- if that file doesn't exist, it loads the default definitions from the installed package (see [src/jsi/config/solvers.json](src/jsi/config/solvers.json))

Based on these definitions, jsi knows what executables to look for, whether a given solver is enabled, how to enable model generation, etc.

Then:
- it looks up the solver cache in `~/.jsi/cache.json`
- if that file doesn't exist, it will scan the PATH and cache the results

It does this because loading cached paths is 4-5x faster than scanning the PATH.

💡 Tip: `~/.jsi/cache.json` can always be safely deleted, jsi will generate it again next time it runs. If you make changes to `~/.jsi/solvers.json` (like adding a new solver), you should delete the cache file, otherwise jsi won't pick up the new solver.
</details>


<details>
<summary>🎨 Rich Output</summary>

jsi uses [rich](https://rich.readthedocs.io/en/stable/) to render nice colored output. However importing rich at startup adds about 30-40ms to jsi's startup time, so by default jsi only uses rich if it detects that its output is a tty.

If you want to minimize jsi's startup time, you can force it to use basic output by redirecting its stderr to a file: `jsi ... 2> jsi.err`
</details>


<details>
<summary>📋 Run a specific sequence of solvers</summary>

Sometimes it can be useful to run only a subset of available solvers, for instance when you already know the top 2-3 solvers for a given problem.

jsi supports a `--sequence` option that allows you to specify a sequence of solvers to run as a comma-separated list of solver names (as defined in your `~/.jsi/solvers.json` file).

![Screenshot of jsi running a sequence of solvers](static/images/jsi-sequence-screenshot.png)
</details>


<details>
<summary>📊 CSV Output</summary>

In addition to the table output, jsi can also output results in CSV format, which is useful for further processing like generating graphs or importing into spreadsheets (especially in conjunction with the `--full-run` option).

```sh
$ jsi --full-run --sequence stp,cvc4,cvc5 --csv examples/unsat-div.smt2
stp returned unsat
cvc4 returned unsat
cvc5 returned unsat
unsat
; (result from stp)

                                   Results
┏━━━━━━━━┳━━━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ solver ┃ result ┃ exit ┃   time ┃ output file                      ┃ size ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ stp    │ unsat  │    0 │  0.01s │ examples/unsat-div.smt2.stp.out  │ 6.0B │
│ cvc4   │ unsat  │    0 │  9.75s │ examples/unsat-div.smt2.cvc4.out │ 6.0B │
│ cvc5   │ unsat  │    0 │ 13.01s │ examples/unsat-div.smt2.cvc5.out │ 6.0B │
└────────┴────────┴──────┴────────┴──────────────────────────────────┴──────┘
writing results to: examples/unsat-div.smt2.csv

$ bat examples/unsat-div.smt2.csv

───────┬─────────────────────────────────────────────────────────────────────
       │ File: examples/unsat-div.smt2.csv
───────┼─────────────────────────────────────────────────────────────────────
   1   │ solver,result,exit,time,output file,size
   2   │ stp,unsat,0,0.01s,examples/unsat-div.smt2.stp.out,6
   3   │ cvc4,unsat,0,9.75s,examples/unsat-div.smt2.cvc4.out,6
   4   │ cvc5,unsat,0,13.01s,examples/unsat-div.smt2.cvc5.out,6
```
</details>


<details>
<summary>🔪 Reaper mode</summary>

jsi makes a best effort to be resilient about crashes and avoid orphaned solver processes. In particular:
- it spawns a reaper thread that checks if the jsi's parent process is still running, and if not, it will kill all solver subprocesses
- it handles keyboard interrupts and SIGTERM
- it can optionally spawn a reaper subprocess that monitors jsi's pid, and if it notices that jsi has died, it will kill any solver subprocesses
</details>


<details>
<summary>🧪 Experimental Daemon Mode</summary>

jsi can also run in daemon mode, where it will start a subprocess to handle requests. This mode is experimental and subject to change.

```sh
# start the daemon with
jsi --daemon

# or
python -m jsi.server

# tail server logs with
tail -f ~/.jsi/daemon/server.{err,out}
```

The daemon will listen for requests on a unix socket, and each request should be a single line containing the path to an smt2 file to solve.

You can then send requests to the daemon:

```sh
# directly with nc
$ echo -n $(pwd)/examples/easy-sat.smt2 | nc -U ~/.jsi/daemon/server.sock
sat
; (result from yices)

# with the included Python client
$ python -m jsi.client examples/easy-sat.smt2
sat
; (result from yices)
```

or for the lowest latency, use the included Rust client:

```sh
# build it
(cd jsi-client-rs && cargo build --release)

# install it
(cd jsi-client-rs && ln -s $(pwd)/target/release/jsif /usr/local/bin/jsif)

# use it
jsif examples/easy-sat.smt2
```

This benchmark shows why you might want to use the Rust client:

```sh
hyperfine --shell=none \
  "python -m jsi.client examples/easy-sat.smt2" \
  "jsif examples/easy-sat.smt2"

Benchmark 1: python -m jsi.client examples/easy-sat.smt2
  Time (mean ± σ):     290.9 ms ±   9.1 ms    [User: 75.7 ms, System: 18.9 ms]
  Range (min … max):   282.3 ms … 313.5 ms    10 runs

Benchmark 2: jsif examples/easy-sat.smt2
  Time (mean ± σ):     196.7 ms ±   4.3 ms    [User: 1.2 ms, System: 2.3 ms]
  Range (min … max):   190.9 ms … 207.2 ms    15 runs

Summary
  jsif examples/easy-sat.smt2 ran
    1.48 ± 0.06 times faster than python -m jsi.client examples/easy-sat.smt2
```

⚠️ **Warning**: the daemon mode is experimental and subject to change. Not all options are supported at this time (like `--sequence`, `--csv`, `--timeout`, etc).
</details>


## Tips

<details>
<summary>Supported solvers</summary>

These solvers have been tested and are known to work with jsi:

    bitwuzla: 0.3.0-dev
    boolector: 3.2.3
    cvc4: 1.8
    cvc5: 1.1.2
    stp: 2.3.3
    yices: 2.6.4
    z3: 4.12.2

</details>


<details>
<summary>Installing solvers</summary>

If you have no solver installed (or even only a single solver installed), jsi will not be particularly useful. It won't install any solvers for you, you need to install them yourself.

If you want to run a Docker image that is already pre-configured with multiple high-performance solvers, check out [our solvers image](https://github.com/a16z/halmos/tree/main/packages/solvers).

</details>


<details>
<summary>Adding new solvers</summary>

To add a new solver, the process is roughly:
- if you already have `~/.jsi/solvers.json`, modify it directly
- if you don't have `~/.jsi/solvers.json`, copy [src/jsi/config/solvers.json](src/jsi/config/solvers.json) to `~/.jsi/solvers.json`
- add the new solver to the `~/.jsi/solvers.json` file (you will need to fill the executable name, the args you want to always pass to the solver, and optionally the model generation option)
- delete `~/.jsi/cache.json` if it exists
- on the next run, jsi will should pick up the new solver

If you wish to contribute a new solver definition to upstream jsi, please check out the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information and submit a PR.

</details>


<details>
<summary>Running multiple versions of the same solver</summary>

Follow the instructions above to add the solver, but use different names for each version.

The only issue is that you will need to make sure the executable names are unique, so that jsi can tell them apart (e.g. `yices-2.6.4` and `yices-2.6.5`).

</details>


<details>
<summary>Disabling a solver</summary>

To avoid running a particular solver:
- temporarily, run with an explicit `--sequence` option that excludes the solver
- permanently, edit `~/.jsi/solvers.json` and set `enabled` to `false`

</details>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)


## Acknowledgements

The setup for this project is based on [postmodern-python](https://rdrn.me/postmodern-python/).


## Disclaimer

_This code is being provided as is. No guarantee, representation or warranty is being made, express or implied, as to the safety or correctness of the code. It has not been audited and as such there can be no assurance it will work as intended, and users may experience delays, failures, errors, omissions or loss of transmitted information. Nothing in this repo should be construed as investment advice or legal advice for any particular facts or circumstances and is not meant to replace competent counsel. It is strongly advised for you to contact a reputable attorney in your jurisdiction for any questions or concerns with respect thereto. a16z is not liable for any use of the foregoing, and users should proceed with caution and use at their own risk. See a16z.com/disclosures for more info._
