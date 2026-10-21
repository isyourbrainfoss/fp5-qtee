# Minimal flake: builds the C session only. It has not been built with Nix
# yet. The Python watcher still hard-codes /home/user paths, so it is not
# packaged. For a full NixOS system see marcusramberg/nixos-fairphone-fp5
# (modules/fingerprint), which this repo credits in README.md.
{
  description = "Fairphone 5 fingerprint over userspace QTEE (focal32)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-linux" "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      packages = forAll (pkgs: rec {
        fp5-qtee-session = pkgs.stdenv.mkDerivation {
          pname = "fp5-qtee-session";
          version = "0-unstable";
          src = ./session;
          makeFlags = [ "CC=${pkgs.stdenv.cc.targetPrefix}cc" ];
          doCheck = true;
          checkTarget = "test";
          installPhase = ''
            install -Dm755 fp5-qtee-session $out/bin/fp5-qtee-session
          '';
          meta = {
            description = "QTEE session for the Fairphone 5 focal32 fingerprint trustlet";
            license = pkgs.lib.licenses.gpl2Only;
            platforms = pkgs.lib.platforms.linux;
          };
        };
        default = fp5-qtee-session;
      });
    };
}
