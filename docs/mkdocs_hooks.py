def on_page_markdown(markdown, page, **kwargs):
    source = page.file.src_uri

    if source in {"index.md", "QUICK_START.md"}:
        markdown = markdown.replace("](assets/docs/", "](")
        if source == "index.md":
            markdown = markdown.replace(
                "](assets/claude-code-plugins/)",
                "](https://github.com/aws-samples/"
                "sample-guidance-for-governed-inference-platform-on-amazon-bedrock/tree/main/"
                "assets/claude-code-plugins/)",
            )
            markdown = markdown.replace(
                "](CONTRIBUTING.md#",
                "](https://github.com/aws-samples/sample-guidance-for-governed-inference-platform-on-amazon-bedrock/blob/main/CONTRIBUTING.md#",
            )
            for root_file in ("LICENSE", "THIRD-PARTY-LICENSES"):
                markdown = markdown.replace(
                    f"]({root_file})",
                    "](https://github.com/aws-samples/"
                    f"sample-guidance-for-governed-inference-platform-on-amazon-bedrock/blob/main/{root_file})",
                )
        return markdown

    replacements = {
        "](../images/": "](assets/images/",
        "](../../assets/images/": "](assets/images/",
        "](../../README.md": "](index.md",
        "](../../QUICK_START.md": "](QUICK_START.md",
    }
    for old, new in replacements.items():
        markdown = markdown.replace(old, new)
    return markdown
