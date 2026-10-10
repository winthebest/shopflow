"""Build SeaweedFS's s3.json at pod start: the lake identities plus the CNPG backup identity.

The lake identities (Secret seaweedfs-s3-config, key s3.json) are one SOPS file generated together with their client
copies; the backup identity has its own Secret (seaweedfs-cnpg-backup, keys access-key-id and secret-access-key) so
it could be added without regenerating the lake's credentials (scripts/data-secrets.sh, group backup). Its actions
live here, next to the bucket it may use. Without that Secret the lake identities are written unchanged.
SeaweedFS reads the file at start, so a changed credential takes effect on the next pod start. Standard library only.
"""

import json
import pathlib

BASE = pathlib.Path("/in/lake/s3.json")
BACKUP = pathlib.Path("/in/cnpg-backup")
OUT = pathlib.Path("/out/s3.json")
BACKUP_BUCKET = "pg-backup"


def main() -> None:
    config = json.loads(BASE.read_text())
    if (BACKUP / "access-key-id").exists():
        config["identities"].append(
            {
                "name": "cnpg-backup",
                "credentials": [
                    {
                        "accessKey": (BACKUP / "access-key-id").read_text().strip(),
                        "secretKey": (BACKUP / "secret-access-key").read_text().strip(),
                    }
                ],
                "actions": [f"Read:{BACKUP_BUCKET}", f"Write:{BACKUP_BUCKET}", f"List:{BACKUP_BUCKET}"],
            }
        )
    OUT.write_text(json.dumps(config))
    OUT.chmod(0o400)
    print(f"s3.json: identities {[identity['name'] for identity in config['identities']]}")


if __name__ == "__main__":
    main()
