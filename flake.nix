{
  description = "What the release set's Windows doc-test legs run against, built and staged on Linux by logos-windows-ci";

  # CI overrides both with what the release set pins (scripts/windows-plan.py),
  # so the lock is only a fallback.
  inputs = {
    logos-module-builder.url = "github:logos-co/logos-module-builder";
    # The released Windows zip itself, not a build of it: the Windows legs
    # validate the artifact users download.
    logosctl-windows = {
      url = "https://github.com/logos-co/logos-logoscore-cli/releases/download/0.3.1/logosctl-x86_64-windows.zip";
      flake = false;
    };
    # basecamp-ui installs into Basecamp's user dir with the released lgpm.
    lgpm-windows = {
      url = "https://github.com/logos-co/logos-package-manager/releases/download/0.3.1/lgpm-x86_64-windows.zip";
      flake = false;
    };
    # basecamp-appimage-smoke installs Basecamp with its released installer.
    basecamp-setup = {
      url = "file+https://github.com/logos-co/logos-basecamp/releases/download/0.3.2/LogosBasecamp-Desktop-v0.3.2-f2fae6-x86_64-setup.exe";
      flake = false;
    };
  };

  # Nix does not run on Windows. logos-windows-ci stages each target X as X/
  # beside the generated script, so these names are the paths the specs' Windows
  # steps use; the probes match the `nix build -o probe-X` links of the other
  # platforms. Basecamp's inspector bundle and test driver come from its own flake
  # at the pinned commit, staged as extra targets (scripts/windows-plan.py).
  outputs = { logos-module-builder, logosctl-windows, lgpm-windows, basecamp-setup, ... }:
    let
      probe = dir: logos-module-builder.lib.mkLogosModule {
        src = dir;
        configFile = dir + "/metadata.json";
        flakeInputs = { inherit logos-module-builder; };
      };
      pkgs = logos-module-builder.inputs.nixpkgs.legacyPackages.x86_64-linux;
    in {
      packages.x86_64-windows = {
        logosctl = pkgs.runCommand "logosctl-x86_64-windows" { } ''
          ln -s ${logosctl-windows} $out
        '';
        lgpm = pkgs.runCommand "lgpm-x86_64-windows" { } ''
          ln -s ${lgpm-windows} $out
        '';
        # A file input is the installer itself; a target has to be a directory.
        # Staged as .bin: logos-windows-ci gates every staged .exe/.dll as a
        # 64-bit build output, and an NSIS installer is a released 32-bit stub.
        # windows.sh copies it back to an .exe to run it.
        basecamp-setup = pkgs.runCommand "basecamp-setup-x86_64-windows" { } ''
          mkdir -p $out
          cp ${basecamp-setup} $out/LogosBasecamp-setup.bin
        '';
        # The same sources the specs write, which check-probes.py keeps in step.
        probe-storage = (probe ./doctests/probes/storage).packages.x86_64-windows.lgx-portable;
        probe-delivery = (probe ./doctests/probes/delivery).packages.x86_64-windows.lgx-portable;
        # Windows has no lock: the pins, and the helper that reads them.
        release-set = pkgs.runCommand "release-set" { } ''
          mkdir -p $out
          cp ${./release-set.json} $out/release-set.json
          cp ${./doctests/windows.sh} $out/windows.sh
        '';
      };
    };
}
