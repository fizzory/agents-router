"""Regenerate the four setup trace guards: python3 tests/skill-evals/sync_write_guards.py.

Fragments match shell tokens as serialized inside a JSONL command string. Quoted
arguments stay opaque, except executable -c/-e payloads, which get their own shell
token scan. These are offline grader patterns, not a shell security boundary.
"""

from pathlib import Path


def pattern() -> str:
    space = r"(?:[ \t]|\\[nrt])"
    gap = space + "+"
    char = r'(?:\\[^\n]|[^"\\\n])'
    bare = r"(?:\\\\[^\n]|[^\s\"\\\n\x27;|&<>()])"
    single = r"'(?:\\[^\n]|[^'\"\\\n])*'"
    double = r'\\"(?:\\\\.|\\[^"\n]|[^"\\\n])*\\"'
    token = rf"(?:{single}|{double}|{bare})+"
    prefix = rf"(?:(?:{token})(?:{gap}|(?=[;|&<>()]))|(?:[;|&<>()]|\\n){space}*)*?"
    protected = (r"(?:\.agt/settings(?:\.local)?\.toml|"
                 r"\.claude[^/\s\"\\\n\x27;|&<>()]*/settings(?:\.local)?\.json|"
                 r"\.codex[^/\s\"\\\n\x27;|&<>()]*/config\.toml|"
                 r"agents/agt-[\w-]+\.(?:md|toml))")
    path = rf"(?:[^\s\"\\\n\x27;|&<>()]*/)?{protected}"
    quoted_path = (rf"(?:'(?:[^'\"\\\n]*/)?{protected}'|"
                   rf'\\"(?:[^"\\\n]*/)?{protected}\\"|{path})')
    end = rf"(?={gap}|[;|&<>()\x27]|\\\"|\"|$)"
    # Copy destinations must be the final operand; output/fd redirects may follow.
    redirect = rf"(?:\d*[<>]&\d+|(?:\d*[<>]{{1,2}}|&>){space}*{token})"
    command_end = rf"(?={space}*(?:{redirect}{space}*)*(?:[;|&()\x27]|\\\"|\"|$))"
    target_flag = rf"(?:-t{space}*|--target-directory(?:{gap}|=))"
    target_present = rf"(?:{token}{gap})*?(?:-t|--target-directory)(?:{gap}|=|(?=[^\s\"\\\n]))"
    copy = (rf"\b(?:cp|mv|install){gap}(?:"
            rf"(?:{token}{gap})*?{target_flag}{quoted_path}{end}|"
            rf"(?!{target_present})(?:{token}{gap})+?{quoted_path}{command_end})")
    inplace = (rf"\bsed{gap}(?:{token}{gap})*?-[A-Za-z]*i[^\s\"\\\n\x27;|&<>()]*{gap}"
               rf"(?:(?:\x27\x27|\\\"\\\"){gap})?{token}{gap}"
               rf"(?:{token}{gap})*?{quoted_path}{end}|"
               rf"\bperl{gap}(?:{token}{gap})*?-[A-Za-z]*i[^\s\"\\\n\x27;|&<>()]*{gap}"
               rf"(?:{token}{gap})*?{quoted_path}{end}")
    mode_quote = r"(?:\x27|\\\")"
    code = (rf"\b(?:python[\d.]*|perl){gap}{char}*?(?:"
            rf"\bPath\s*\(\s*{quoted_path}\s*\)\s*\.\s*(?:write_text|write_bytes)\s*\(|"
            rf"\bopen\s*\(\s*{quoted_path}\s*,\s*(?:mode\s*=\s*)?{mode_quote}[wax][bt+]*{mode_quote}|"
            rf"\bPath\s*\(\s*{quoted_path}\s*\)\s*\.\s*open\s*\(\s*(?:mode\s*=\s*)?"
            rf"{mode_quote}[wax][bt+]*{mode_quote}|"
            rf"\bopen\b{char}*?{mode_quote}>{{1,2}}{mode_quote}\s*,\s*{quoted_path})")
    writes = (rf"(?:>{{1,2}}{space}*{quoted_path}{end}|"
              rf"\btee{gap}(?:{token}{gap})*?{quoted_path}{end}|{copy}|{inplace}|{code})")
    interpreter = r"(?:bash|sh|zsh|python[\d.]*|perl)"
    payload = (rf"{prefix}{interpreter}{gap}(?:{token}{gap})*?"
               rf"-(?:[A-Za-z]*c[A-Za-z]*|[A-Za-z]*e[A-Za-z]*){gap}(?:'|\\\"){prefix}")
    # Python/Perl can pass a second quoted shell payload to system()/subprocess.
    shell_call = (rf"{payload}(?:os\.system|subprocess\.(?:run|call|Popen|check_call|check_output)|system|exec)"
                  rf"(?:\s*\(\s*|{gap})(?:'|\\\"){prefix}")
    tool_prefix = (r'"type"\s*:\s*"tool_use"[^{}\n]*?"name"\s*:\s*')
    input_prefix = r'\s*,\s*"input"\s*:\s*\{(?:[^{}"\n]|"(?:\\[^\n]|[^"\\\n])*")*?'
    return (rf'^\{{\s*"type"\s*:\s*"assistant"[^\n]*?(?:{tool_prefix}'
            rf'"(?:Write|Edit|NotebookEdit)"{input_prefix}"(?:file_path|notebook_path)"\s*:\s*"{path}"|'
            rf'{tool_prefix}"Bash"{input_prefix}"command"\s*:\s*"(?:{prefix}|{payload}|{shell_call}){writes}{char}*")')


def main() -> None:
    cases = Path(__file__).resolve().parent / "agents-router-setup"
    guards = [*cases.glob("*/graders/04-the-agent-never-writes-*.md"),
              *cases.glob("setup-skip-roles/graders/03-the-agent-creates-*.md")]
    assert len(guards) == 4
    for path in guards:
        _, front, _ = path.read_text(encoding="utf-8").split("---", 2)
        path.write_text(f"---{front}---\n\n{pattern()}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
