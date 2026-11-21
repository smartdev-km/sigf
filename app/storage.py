"""Stockage des fichiers numérisés.

- En local : disque (instance/uploads).
- En production : S3-compatible (Cloudflare R2, Backblaze B2…). Les fichiers ne sont
  jamais publics : l'accès passe toujours par l'application (contrôle des droits),
  puis par un lien signé valable 60 secondes.
"""

import os
import shutil

from flask import current_app, redirect, send_file


class StorageError(Exception):
    pass


class LocalStorage:
    def __init__(self, racine):
        self.racine = os.path.abspath(racine)

    def _chemin(self, cle):
        chemin = os.path.abspath(os.path.join(self.racine, cle))
        if not chemin.startswith(self.racine + os.sep):
            raise StorageError("Clé de stockage invalide.")
        return chemin

    def put(self, cle, data, mime):
        chemin = self._chemin(cle)
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "wb") as f:
            f.write(data)

    def lire(self, cle):
        try:
            with open(self._chemin(cle), "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise StorageError("Fichier introuvable dans le stockage.") from None

    def delete(self, cle):
        try:
            os.remove(self._chemin(cle))
        except FileNotFoundError:
            pass

    def copier(self, source, destination):
        origine = self._chemin(source)
        if not os.path.exists(origine):
            raise StorageError("Fichier introuvable dans le stockage : copie impossible.")
        cible = self._chemin(destination)
        os.makedirs(os.path.dirname(cible), exist_ok=True)
        shutil.copyfile(origine, cible)

    def reponse(self, cle, nom, mime, inline):
        chemin = self._chemin(cle)
        if not os.path.exists(chemin):
            raise StorageError("Fichier introuvable dans le stockage.")
        return send_file(chemin, mimetype=mime, as_attachment=not inline, download_name=nom)


class S3Storage:
    def __init__(self, bucket, endpoint_url, access_key, secret_key, region):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or "auto",
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def put(self, cle, data, mime):
        try:
            self.client.put_object(Bucket=self.bucket, Key=cle, Body=data, ContentType=mime)
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"Échec de l'envoi vers le stockage : {e.__class__.__name__}") from e

    def lire(self, cle):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=cle)["Body"].read()
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"Lecture impossible dans le stockage : {e.__class__.__name__}") from e

    def copier(self, source, destination):
        try:
            self.client.copy_object(Bucket=self.bucket, Key=destination, CopySource={"Bucket": self.bucket, "Key": source})
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"Copie impossible dans le stockage : {e.__class__.__name__}") from e

    def delete(self, cle):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=cle)
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Suppression S3 impossible pour %s", cle)

    def reponse(self, cle, nom, mime, inline):
        disposition = "inline" if inline else "attachment"
        nom_sur = nom.replace('"', "").replace("\r", "").replace("\n", "")
        url = self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": cle,
                "ResponseContentType": mime,
                "ResponseContentDisposition": f'{disposition}; filename="{nom_sur}"',
            },
            ExpiresIn=60,
        )
        return redirect(url)


def get_storage():
    app = current_app
    if "storage" in app.extensions:
        return app.extensions["storage"]
    cfg = app.config
    if cfg.get("S3_BUCKET"):
        stockage = S3Storage(
            cfg["S3_BUCKET"],
            cfg.get("S3_ENDPOINT_URL"),
            cfg.get("S3_ACCESS_KEY_ID"),
            cfg.get("S3_SECRET_ACCESS_KEY"),
            cfg.get("S3_REGION"),
        )
    elif os.environ.get("RENDER") and not app.testing:
        # Le disque de Render est éphémère : on refuse plutôt que de perdre des scans.
        raise StorageError(
            "Le stockage des fichiers n'est pas configuré (variables S3_*). "
            "Le disque de Render est effacé à chaque déploiement."
        )
    else:
        racine = cfg.get("UPLOAD_DIR") or os.path.join(app.instance_path, "uploads")
        stockage = LocalStorage(racine)
    app.extensions["storage"] = stockage
    return stockage
