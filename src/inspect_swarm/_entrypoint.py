# Loaded by inspect_ai through the `inspect_ai` entry point when it looks up an
# `inspect_swarm/<name>` it has not seen, so that names resolve without
# importing the package first. The built-in controllers and channels are
# imported here as they are added.
from ._channel.filesystem import filesystem as filesystem
from ._channel.shared_memory import shared_memory as shared_memory
from ._controller.leaderless import leaderless as leaderless
