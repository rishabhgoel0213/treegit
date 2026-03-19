class TreeGitError(Exception):
    """Base TreeGit error."""


class RepoNotFoundError(TreeGitError):
    """Raised when the current directory is not inside a TreeGit repository."""


class RepoExistsError(TreeGitError):
    """Raised when a repository already exists."""


class InvalidObjectError(TreeGitError):
    """Raised when an object cannot be parsed or is missing."""


class DirtyWorkingTreeError(TreeGitError):
    """Raised when an operation requires a clean working tree."""


class CheckoutConflictError(TreeGitError):
    """Raised when checkout would overwrite protected paths."""


class UnsupportedFileError(TreeGitError):
    """Raised when the working tree contains an unsupported special file."""


class ReferenceResolutionError(TreeGitError):
    """Raised when a ref or commit prefix cannot be resolved."""


class BranchExistsError(TreeGitError):
    """Raised when creating a branch that already exists."""


class BranchNavigationError(TreeGitError):
    """Raised when checkout targets a non-adjacent branch."""


class MetricExistsError(TreeGitError):
    """Raised when creating a metric that already exists."""


class MetricNotFoundError(TreeGitError):
    """Raised when reading or updating an unknown metric."""
