# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. If your change warrants a version bump, update **both** `source/pyproject.toml` and `CHANGELOG.md` in the same PR.
5. Commit to your fork using clear commit messages.
6. Send us a pull request, answering any default questions in the pull request interface.
7. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Running the validation suite in your own CI

Deployment never requires GitHub: the documented path is
`git clone` (or a tarball / internal mirror) → `poetry install` → `gip init` →
`gip deploy` → `gip package`, all from an admin workstation. The GitHub Actions
workflows in this repository are fork/maintainer CI only.

If you fork this repository to customize templates and want pre-merge validation
in GitLab, CodeCatalyst, Bitbucket, or any other CI system, the checks are plain
vendor-agnostic commands:

```bash
# 1. Python tests (from source/)
cd source && poetry install && poetry run pytest tests/ -q

# 2. Go tests (from source/go/)
cd source/go && go test ./... -count=1

# 3. CloudFormation lint (from the repo root; ignore list matches CI —
#    see .github/workflows/pytest-ci.yml for the authoritative flags)
pip install cfn-lint && cfn-lint deployment/infrastructure/*.yaml \
  --ignore-checks W3002 W2001 E3012 E3005 E0000 W1001 W1028 W1030 W2010 W2531 W3005 W3011 W3037 W8001

# 4. Python lint/format
pip install ruff && ruff check . && ruff format --check .

```

Expected pass criteria: all four commands exit zero. Note that full parity with
upstream CI also requires running the Python tests on a **Windows** runner
(Windows failures are blocking upstream); if your CI has no Windows runners, the
repository's `codebuild-windows` setup runs them on AWS CodeBuild instead.

### Smoke-testing a package in a clean Linux container

`gip package` writes bundles to `dist/<profile>/<timestamp>/` relative to the
directory it runs from (normally `source/`). To try an installer on a stock
Ubuntu image without touching your workstation, mount that directory read-only
and work interactively (port 8400 is the OAuth callback):

```bash
# from the repository root
docker run --rm -it --security-opt no-new-privileges:true -p 8400:8400 \
  -v "$PWD/source/dist:/package:ro" \
  public.ecr.aws/docker/library/ubuntu@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc bash
# inside the container: apt-get update && apt-get install -y unzip python3 curl,
# then copy a bundle out of /package and run its install.sh
```


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
