# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""GitHub/Webex prompt templates for webhook-triggered autonomous tasks.

The templates in this module are canned prompts for common GitHub
webhook flows. They are not webhook registration code; they produce the
prompt passed to ``create_autonomous_task`` after the operator selects a
scenario such as ``github_issue_triage``.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool


TemplateName = Literal[
    "github_issue_triage",
    "github_pr_review",
    "github_push_notify",
]


_WEBHOOK_CONTEXT_PREAMBLE = """
You are running as a scheduled autonomous task that fires when a
webhook arrives. The GitHub webhook payload is appended to this prompt
under `Context:` as JSON. Parse it to get the event details you need;
do NOT ask the operator for fields that are in the payload.

If the payload is malformed or missing required fields, post a short
error message to the Webex space ({webex_room_ref}) and stop. Do not
try to guess values.
""".strip()


GITHUB_ISSUE_TRIAGE = """
A new issue has just been opened on the GitHub repository {repo}.

{preamble}

Do the following, in this order:

Step 1 - Acknowledge on Webex:
  Post a short message to the Webex space `{webex_room_ref}` saying
  you have received the issue. Include:
    * the issue number (issue.number)
    * the issue title (issue.title)
    * a short link (issue.html_url)
    * the opener's username (issue.user.login)
    * a one-line "I am starting investigation" note
  Keep it under 5 lines.

Step 2 - Investigate the issue:
  Use the github sub-agent tools to gather context at depth
  "{investigation_depth}":
    * Read the issue body in full.
    * If the body references files, read the current contents of those
      files at the default branch.
    * Look for recent commits that touched files or directories
      mentioned in the issue.
    * Search closed issues in the same repo for duplicates by key terms
      from the title. Report duplicates if found.
  Build a concise understanding of what the reporter is saying, what
  the code does around the relevant lines, and whether this is a known
  problem.

Step 3 - Report to Webex:
  Post a second message to `{webex_room_ref}` with:
    * Your working understanding of the bug or request
    * The specific file(s) and line range(s) most relevant
    * Any duplicate or related issues found, with links
    * A proposed next action
  Use Webex markdown. Keep it to 15 lines max.

Step 4 - Reply on the GitHub issue:
  Post a comment on the original GitHub issue with the same summary
  from Step 3, formatted as GitHub-flavoured markdown.
  Sign the comment "Posted by CAIPE auto-triage" so readers know this
  is an automated first response.

If any step fails, post a short explanation to `{webex_room_ref}` and
stop. Do not continue downstream steps with partial information.
""".strip()


GITHUB_PR_REVIEW = """
A new pull request has just been opened on the GitHub repository
{repo}.

{preamble}

Do the following, in this order:

Step 1 - Acknowledge on Webex:
  Post to `{webex_room_ref}` with:
    * the PR number (pull_request.number)
    * the title (pull_request.title)
    * the opener (pull_request.user.login)
    * the branch info (pull_request.head.ref -> pull_request.base.ref)
    * a one-line "starting review" note
  Keep it under 5 lines.

Step 2 - Read the diff:
  Use the github sub-agent to fetch the changed files. At depth
  "{investigation_depth}":
    * Summarise what each changed file does before vs after.
    * Flag breaking API changes.
    * Flag secrets or credentials added inline.
    * Flag obvious correctness issues such as unhandled exceptions,
      resource leaks, or unused imports.
  Focus on correctness and safety, not style preferences.

Step 3 - Report to Webex:
  Post a review summary to `{webex_room_ref}` including:
    * High-level description of the change
    * Any red flags found
    * Recommendation: "looks good", "needs changes", or "needs discussion"

Step 4 - Reply on the GitHub PR:
  Post a regular PR comment summarising your findings.
  Sign "Posted by CAIPE auto-review".
  DO NOT approve or reject the PR; leave merge judgement to a human.
""".strip()


GITHUB_PUSH_NOTIFY = """
A push has just landed on the GitHub repository {repo}.

{preamble}

Post ONE message to the Webex space `{webex_room_ref}` summarising:
  * the branch (ref, stripped of `refs/heads/`)
  * the pusher's username (pusher.name or sender.login)
  * the number of commits (len(commits))
  * a bulleted list of at most 5 commit summaries, each showing the
    commit's short SHA, author, and first commit-message line
  * a link to the compare URL (compare)

Keep it compact. No further investigation is needed.
""".strip()


_TEMPLATES: dict[str, str] = {
    "github_issue_triage": GITHUB_ISSUE_TRIAGE,
    "github_pr_review": GITHUB_PR_REVIEW,
    "github_push_notify": GITHUB_PUSH_NOTIFY,
}


@tool
def get_webhook_task_template(
    template_name: TemplateName,
    repo: str,
    webex_room_ref: str,
    investigation_depth: Literal["shallow", "standard", "deep"] = "standard",
) -> str:
    """Return a GitHub/Webex webhook task prompt with parameters filled in.

    Current templates are opinionated GitHub workflows that notify a
    Webex space. Use them as a starting point for
    ``create_autonomous_task(prompt=...)``; for GitHub-only or
    non-Webex workflows, write or adapt a custom prompt.

    Args:
        template_name: One of ``github_issue_triage``,
            ``github_pr_review``, or ``github_push_notify``.
        repo: Repository watched by the webhook, in ``owner/name`` form.
        webex_room_ref: Webex room id or human-readable room reference.
        investigation_depth: How much context to gather: ``shallow``,
            ``standard``, or ``deep``.
    """
    if template_name not in _TEMPLATES:
        return (
            f"Unknown template '{template_name}'. Available: "
            + ", ".join(sorted(_TEMPLATES.keys()))
        )
    if not repo or not isinstance(repo, str):
        return "Parameter 'repo' must be a non-empty 'owner/name' string."
    if not webex_room_ref or not isinstance(webex_room_ref, str):
        return (
            "Parameter 'webex_room_ref' must be a non-empty string "
            "(Webex roomId or a human reference the task can resolve)."
        )

    preamble = _WEBHOOK_CONTEXT_PREAMBLE.format(webex_room_ref=webex_room_ref)
    return _TEMPLATES[template_name].format(
        preamble=preamble,
        repo=repo,
        webex_room_ref=webex_room_ref,
        investigation_depth=investigation_depth,
    )
