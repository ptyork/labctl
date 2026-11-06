# git_wrap 0.2 notes for labctl

git_wrap 0.2 implements what `../LABCTL_GIT_PLAN.md` §8 asked for: all the §8.2 fixes,
and the §8.1 API under the names below. The general change list is in
`../../git_wrap/CHANGELOG.md`; this note covers only what matters when writing
labctl's `ConfigRepo` adapter.

## Proposed API vs. what was built

`LABCTL_GIT_PLAN.md` §8.1 (and the former `git_wrap/NOTES_LABCTL_AND_GH.md`) proposed
an API before implementation. Names follow git_wrap's existing style
(`get_*`, `list_*`), and a few proposals were folded into existing methods:

| Proposed | Use instead | Notes |
|---|---|---|
| `Result(code, stdout, stderr)` | `subprocess.CompletedProcess[str]` | Fields are `returncode`, `stdout`, `stderr`. |
| `repo.run(*args, check=False, input=None, env=None, timeout=None)` | `repo.run(...)` (same signature) | Also `GitRepo.git(*args, path=...)` with no object. `network=True` disables prompts. |
| `rev_parse(rev) -> str \| None` | `resolve_commit(rev)` | Peels tags to the commit. |
| `is_ancestor(a, b="HEAD")` | `is_ancestor(ancestor, rev="HEAD")` | Raises `GitCommandError` if a rev is unknown. |
| `ahead_behind(rev, upstream)` | `get_ahead_behind(rev, upstream)` | Returns `(ahead, behind)`. |
| `prefix_of(path)` | `relative_path(path)` | posix, `""` at the root, `ValueError` outside the repo. |
| `status(*paths) -> list[StatusEntry]` | `list_changes(*paths)` | `StatusEntry(path, index, worktree, orig_path)`, plus `is_staged`, `is_unstaged`, `is_untracked`. Untracked files are listed individually. `get_status(*paths)` gives counts. |
| `ls_files(*paths, untracked=False)` | `list_files(*paths, untracked=False)` | |
| `commit(message, paths=None) -> str` | same | With `paths`, stages them (including deletions) and commits only those paths. |
| `export(dest, rev, path, umask)` | same | |
| `show_file(rev, path) -> bytes \| None` | same | `None` if the path is missing at `rev`; raises if `rev` is invalid. |
| `create_tag(name, rev, message)` | same, plus `force=` | Annotated if `message` is given, lightweight otherwise. |
| `tags(pattern, sort="-refname", count)` | `list_tags(pattern=None, sort="-creatordate", limit=None)` | `Tag(name, commit, tagger_name, tagger_email, date, message)`, plus `is_annotated`. `commit` is the peeled commit. `pattern` is a glob (`"v1.*"`) or a prefix (`"deploy/"`). |
| `delete_tag(name)` | same | |
| `fetch(remote, refspecs, prune, timeout)` | same | Returns `None` and raises on failure. `refspecs` requires `remote`. |
| `push_refs(remote, *refspecs, atomic=True)` | same, plus `force=`, `timeout=` | |
| `identity() -> (name, email)` | `GitRepo.get_user_name(path)`, `GitRepo.get_user_email(path)` | All scopes, including repo-level config. |
| `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` | not set | It would override the user's `core.sshCommand`. Instead, network calls get `GIT_TERMINAL_PROMPT=0` and stdin from `/dev/null`, and are detached from the terminal (POSIX), so ssh fails instead of prompting. `interactive=True` opts out. |
| Rename `Remote` → `GitRemote` | not renamed | Use `import git_wrap as gw` / `gw.Remote`. |
| `git_wrap.utils.for_each_repo(root, *args) -> dict[Path, Result]` | `git_wrap.for_each_repo(path, *args)` | Returns `dict[Path, CompletedProcess[str]]` for direct subdirectories that are repos. |
| `gh.gh(*args)`, `gh.whoami()`, `gh.auth_token()`, `gh.gitignore_template()` | `gh.run()`, `gh.get_login()`, `gh.get_auth_token()`, `gh.get_gitignore_template()` | `gh.create_repo()` never deletes on failure; call `gh.delete_repo()` yourself. |

## Behavior worth knowing

- **Errors:** every failure raises a `CommandError` subclass with `argv`, `cwd`,
  `returncode`, `stdout`, `stderr`. Nothing returns `False` to signal an error;
  boolean returns are real answers (`stash()` means "something was stashed",
  `pull()` means "HEAD moved").
- **Timeouts** raise `subprocess.TimeoutExpired`, not `GitCommandError`. The child
  process is killed first.
- **Ctrl-C** propagates as `KeyboardInterrupt`, and the git child process is killed.
- **Paths** given to `add`, `commit`, `list_changes`, `list_files`, `show_file`,
  `export` and `get_commits` are relative to the repository root if relative, and
  converted to it if absolute. `relative_path()` alone resolves relative paths
  against the current directory.
- **Revisions** starting with `-` raise `ValueError`, so they are never parsed as options.
- **`export()`** uses `git archive`, so only committed content is written: no
  `.git`, untracked or ignored files. With `path`, the subtree's contents go directly
  into `dest`. It overwrites matching files but does **not** delete other files
  already in `dest`; export into an empty directory and swap it in for an exact copy.
  Symlinks that point outside `dest` are rejected (Python's `tarfile` data filter).
  An empty tree exports to an empty directory.
- **Cost:** constructing `GitRepo` runs one git command. `pull()` runs three (HEAD
  before and after); `commit()` runs two (three with `paths`), and `stash_pop()` runs
  two. Everything else runs one.
- **Minimum versions:** git 2.28. For `git_wrap.gh`, gh 2.40 (`gh auth token` and
  `repo delete --yes` don't exist in Ubuntu's gh 2.4).
