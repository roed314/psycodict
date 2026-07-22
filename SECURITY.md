# Security Policy

## Supported versions

Only the latest published version of psycodict receives security fixes. Users
should upgrade to the newest release before reporting a vulnerability.

Versions earlier than 1.0 are not supported.

## Reporting a vulnerability

Please do not report suspected security vulnerabilities through a public GitHub
issue, discussion, or pull request.

Instead, use GitHub's private vulnerability reporting feature from the
repository's **Security** tab. If private reporting is unavailable, contact
roed@mit.edu.

Please include as much of the following information as possible:

- The affected psycodict version or commit.
- Your Python, PostgreSQL, and database-driver versions.
- A description of the security impact.
- A minimal reproduction or proof of concept.
- The psycodict operation or query that triggers the problem.
- Relevant generated SQL or logs, with credentials and private data removed.
- Any known mitigations or workarounds.

Do not include production credentials, connection strings, or private database
contents in the report.

## Scope

Examples of security issues that are in scope include:

- SQL injection through psycodict's normal dictionary query interface.
- Unauthorized reads, writes, or privilege escalation.
- Exposure of database credentials or other secrets.
- Unsafe handling of configuration files or log files.
- Data corruption or unintended destructive database operations.
- Vulnerabilities in psycodict's packaging or release process.

The `$raw` query operator accepts trusted SQL fragments and must not be used
with untrusted input. Injection caused solely by deliberately passing untrusted
input through `$raw` is not considered a psycodict vulnerability. A way to
inject SQL through an interface that is intended to be safe is in scope.

Problems in PostgreSQL, psycopg, or another dependency should normally be
reported to that project unless psycodict uses the dependency unsafely.
Ordinary bugs without a security impact may be reported through the public
issue tracker.

## Response and disclosure

We aim to acknowledge reports within three business days and provide an initial
assessment within seven days. These are targets rather than guaranteed
resolution times.

We ask reporters to allow reasonable time for investigation and release of a
fix before public disclosure. When appropriate, we will coordinate publication
of a GitHub security advisory, release a patched version, and credit the
reporter unless anonymity is requested.

## Good-faith research

Please conduct testing against systems and data you own or are authorized to
use. Avoid accessing unnecessary data, disrupting services, or modifying or
destroying data.

We will not pursue legal action against researchers who follow this policy,
act in good faith, and give us a reasonable opportunity to address the issue.

## AI usage

This document was drafted by GPT 5.6.