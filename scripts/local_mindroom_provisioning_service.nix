{ config, lib, pkgs, ... }:

let
  cfg = config.services.mindroom-local-provisioning;
in
{
  options.services.mindroom-local-provisioning = {
    enable = lib.mkEnableOption "MindRoom local provisioning service";

    repoPath = lib.mkOption {
      type = lib.types.str;
      default = "/srv/mindroom";
      description = "Absolute path to a checkout of this repository.";
    };

    scriptPath = lib.mkOption {
      type = lib.types.str;
      default = "scripts/local_mindroom_provisioning_service.py";
      description = "Path to the provisioning script relative to repoPath.";
    };

    matrixHomeserver = lib.mkOption {
      type = lib.types.str;
      default = "https://mindroom.chat";
      description = "Matrix homeserver used for /account/whoami token verification.";
    };

    matrixServerName = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional Matrix server_name override when it differs from matrixHomeserver host.";
    };

    matrixRegistrationTokenFile = lib.mkOption {
      type = lib.types.str;
      description = "File containing the Matrix registration token.";
    };

    listenHost = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Bind address for the local provisioning HTTP server.";
    };

    listenPort = lib.mkOption {
      type = lib.types.port;
      default = 8776;
      description = "Bind port for the local provisioning HTTP server.";
    };

    corsOrigins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "https://chat.mindroom.chat" ];
      description = "CORS origins allowed to call provisioning endpoints from browser UI.";
    };

    statePath = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/mindroom-local-provisioning/state.json";
      description = "State file path for pair sessions/connections.";
    };

    caddyHost = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional host name to publish through Caddy (for example provisioning.mindroom.chat).";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.mindroom-local-provisioning = {
      isSystemUser = true;
      group = "mindroom-local-provisioning";
      home = "/var/lib/mindroom-local-provisioning";
    };
    users.groups.mindroom-local-provisioning = { };

    systemd.tmpfiles.rules = [
      "d /var/lib/mindroom-local-provisioning 0750 mindroom-local-provisioning mindroom-local-provisioning -"
    ];

    systemd.services.mindroom-local-provisioning = {
      description = "MindRoom Local Provisioning Service";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        MATRIX_HOMESERVER = cfg.matrixHomeserver;
        MATRIX_REGISTRATION_TOKEN_FILE = cfg.matrixRegistrationTokenFile;
        MINDROOM_PROVISIONING_HOST = cfg.listenHost;
        MINDROOM_PROVISIONING_PORT = toString cfg.listenPort;
        MINDROOM_PROVISIONING_STATE_PATH = cfg.statePath;
        MINDROOM_PROVISIONING_CORS_ORIGINS = lib.concatStringsSep "," cfg.corsOrigins;
      } // lib.optionalAttrs (cfg.matrixServerName != null) {
        MATRIX_SERVER_NAME = cfg.matrixServerName;
      };

      serviceConfig = {
        Type = "simple";
        User = "mindroom-local-provisioning";
        Group = "mindroom-local-provisioning";
        WorkingDirectory = cfg.repoPath;
        ExecStart = "${pkgs.uv}/bin/uv run --script ${cfg.repoPath}/${cfg.scriptPath}";
        Restart = "on-failure";
        RestartSec = "5s";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ "/var/lib/mindroom-local-provisioning" ];
      };
    };

    services.caddy.virtualHosts = lib.mkIf (cfg.caddyHost != null) {
      "${cfg.caddyHost}" = {
        extraConfig = ''
          reverse_proxy localhost:${toString cfg.listenPort}
        '';
      };
    };
  };
}
