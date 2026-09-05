{
  description = "fulcrumaxe — reproducible dev environment (Python + Node/TS + Bun)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python312        # backend/: fastapi, pydantic, duckdb, textual (requirements.txt)
          nodejs_22        # dashboard/ (vite+react), tui/ (ts-node+react)
          bun              # ts-backend/: `bun run|build|test`, @duckdb/node-api
          git
          jq               # team orchestration scripts (spawn, setup-state-dir, hooks)
          ruff             # linter referenced by the Makefile
          sqlite           # .autonomous-team/state.db and friends
          duckdb           # stats.duckdb metrics store (backend/stats_writer.py)
          rustc            # archived Rust perf component under archive/ (optional, `make test`)
          cargo
        ];

        # Runtime libs that compiled Python wheels (duckdb, pydantic-core, ...)
        # link against. NixOS doesn't put these on a global path like Ubuntu does.
        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib   # libstdc++ / libgcc
          pkgs.zlib
        ];

        shellHook = ''
          # Create a local virtualenv from requirements.txt on first entry.
          if [ ! -d .venv ]; then
            echo "[flake] creating .venv from requirements.txt (first run only)..."
            python -m venv .venv
            .venv/bin/pip install --quiet --upgrade pip
            .venv/bin/pip install --quiet -r requirements.txt
          fi
          source .venv/bin/activate
          echo "[flake] fulcrumaxe dev env ready — node $(node -v), bun $(bun -v), $(python -V)"
        '';
      };
    };
}
