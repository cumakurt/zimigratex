# Security notes

Mailbox exports, password hashes, filter scripts, forwarding addresses, signatures, and
data-source credentials are sensitive. Keep the archive on trusted encrypted storage,
limit operating-system access, protect the passphrase separately, and securely dispose
of both after the migration retention period.

The tool does not execute commands through SSH or another remote shell. Source export
and destination import run only against the Zimbra installation on the current machine.
It uses shell-free subprocess execution and runs local Zimbra administrative commands
under the `zimbra` operating-system account. Sensitive provisioning values, including
account create and password-hash restore, are supplied through `zmprov`'s stdin batch
mode instead of process arguments, preventing them from appearing in local process
listings. Password hashes are written with LDAP-direct `zmprov -l`; mailboxd account
cache is then flushed over SOAP so restored credentials are used immediately.

Interactive archive passphrases are read with `getpass` and passed directly to the
archive layer. For non-interactive use, the configured passphrase environment variable
is removed from the zimigrate process before any Zimbra child process is started.

Plaintext mailbox archive data exists temporarily while Zimbra produces or consumes one
artifact. Temporary files are mode `0600`, are removed after each operation, and live
under the archive's mode `0700` `.tmp` directory. Place the archive on an encrypted
filesystem if plaintext remnants in storage are in scope for your threat model.

Do not enable unencrypted archives in production. `allow_unencrypted = true` is an
explicit escape hatch intended only for controlled testing and cannot be combined with
secret export.

Global/server configuration import requires an explicit allowlist. Sensitive attributes
are additionally blocked unless `allow_sensitive_config = true`; enabling it requires a
manual security and topology review.

Encrypted provisioning records, mailbox payloads, and the authoritative
`.manifest.zmenc` are authenticated with AES-GCM. `manifest.json` remains readable for
operators but is not trusted when the encrypted manifest is present. Preserve all hidden
files during transfer. An encrypted legacy archive without `.manifest.zmenc` must be
resumed on its source before it is accepted for import; signing an untrusted plaintext
manifest after transfer would not provide provenance.

Zimbra stores data-source credential fields with a legacy, data-source-ID-bound
encoding. When secret export is enabled, zimigrate decrypts those fields in memory so
the target can encrypt them against its newly assigned data-source IDs. The compatibility
path is decrypt-only; plaintext values are written only inside the authenticated AES-GCM
archive and are redacted from command diagnostics.
