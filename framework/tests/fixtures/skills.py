def write_skill(path, text):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text('---\nname: x\ndescription: d\n---\n' + text, encoding='utf-8')
    return path
