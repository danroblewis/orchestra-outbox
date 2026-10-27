"""The Outbox: drafts that machines write and the owner approves before anything is sent as the
owner. See SPEC.md."""

PACKAGE = "outbox"         # the plugin's name: its URLs, its process's prefix, its machine's package
MACHINE = "outbox"         # the machine's id: the source its sends and its policy's decisions publish as
DEFAULT_OWNER = "owner"    # the source the owner's approvals and rejections publish as, unless configured
