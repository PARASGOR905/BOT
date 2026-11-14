#!/usr/bin/env python3

import argparse
import os
import sys
from pymediainfo import MediaInfo


def main():
    # Reconfigure stdout/stderr to use UTF-8 to prevent encoding errors on Windows console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Check MediaInfo of a particular media file."
    )
    parser.add_argument(
        "file",
        help="Path to the media file to analyze."
    )
    parser.add_argument(
        "-f", "--format",
        choices=["text", "json", "xml"],
        default="text",
        help="Output format (default: text)."
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to save the output. If not provided, prints to stdout."
    )

    args = parser.parse_args()

    # Verify input file exists
    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.file):
        print(f"Error: Path is not a file: {args.file}", file=sys.stderr)
        sys.exit(1)

    # Determine parse output format parameter
    parse_format = "STRING"
    if args.format == "json":
        parse_format = "JSON"
    elif args.format == "xml":
        parse_format = "XML"

    try:
        # Extract media info
        info = MediaInfo.parse(args.file, output=parse_format)

        if args.output:
            # Create directories for output if they don't exist
            out_dir = os.path.dirname(args.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(info)
            print(f"MediaInfo successfully saved to: {args.output}")
        else:
            print(info)
    except Exception as e:
        print(f"Error parsing media info: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
