# Security notes

Mailbox exports, password hashes, filter scripts, forwarding addresses, signatures, and
data-source credentials are stored in plaintext in the archive. Keep the archive on
trusted encrypted storage, limit operating-system access, and securely dispose of it
after the migration retention period.

The default export and import run only against the Zimbra installation on the current
machine. `export --target-ip` is opt-in SSH: OpenSSH and rsync copy this toolkit to the
Zimbra host, run local `zmprov` there, and copy each completed mailbox artifact to the
operator workstation before deleting it on the Zimbra host. Key authentication is tried
first. A password is requested only when key login fails, is stored in a mode `0600`
temporary file for OpenSSH ASKPASS, and is never passed as a process argument. TOML
`[source]` / `[target]` SSH settings remain rejected.

It uses shell-free subprocess execution and runs local Zimbra administrative commands
under the `zimbra` operating-system account. Sensitive provisioning values, including
account create and password-hash restore, are supplied through `zmprov`'s stdin batch
mode instead of process arguments, preventing them from appearing in local process
listings. Password hashes are written with LDAP-direct `zmprov -l`; mailboxd account
cache is then flushed over SOAP so restored credentials are used immediately.

Plaintext mailbox archive data exists temporarily while Zimbra produces or consumes one
artifact. Temporary files are mode `0600`, are removed after each operation, and live
under the archive's mode `0700` `.tmp` directory. Remote export copies each completed
artifact to the operator workstation over SSH and then deletes it on the Zimbra host.
Place the archive on an encrypted filesystem if plaintext remnants in storage are in
scope for your threat model.

Mailbox payloads and provisioning records are checksummed with SHA-256. `manifest.json`
is the archive inventory and `state.sqlite3` binds each provisioning record to its
digest; both are required. Validation rejects missing, changed, duplicate, or
unreferenced artifacts before import. Preserve the complete directory during transfer.
These digests detect accidental corruption but are not a cryptographic signature against
an attacker who can rewrite the archive and checkpoint database. Encrypted archives from
older zimigrate builds (`.zmenc`, `salt.bin`, `.keycheck`) are rejected.

Zimbra stores data-source credential fields with a legacy, data-source-ID-bound
encoding. When secret export is enabled, zimigrate decrypts those fields in memory so
the target can encrypt them against its newly assigned data-source IDs. The compatibility
path is decrypt-only; plaintext values are written into the local archive and are
redacted from command diagnostics. On import, each destination data source remains
disabled until all stored settings and credentials have been applied.

Mapped remote mailbox hosts are rejected by default because their volume free space is
not visible to the local process. `allow_unverified_remote_capacity = true` records an
explicit operational exception; check every remote message and index volume first.
