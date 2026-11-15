"""Typed, lossless I/O addresses. Different naming schemes never alias implicitly."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from string import Formatter

_CUSTOM=[]
_RULES_SIGNATURE='builtin-v1'


def load_rules(path=None):
    """Load trusted local naming profiles; no Python callbacks or eval expressions."""
    global _CUSTOM, _RULES_SIGNATURE
    loaded=[]
    if path is not None:
        from pathlib import Path
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        names=set()
        for profile in data.get('profiles',[]):
            name=profile['name']
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}',name) or name in names:
                raise ValueError('命名规则名称非法或重复')
            names.add(name)
            for kind,template in profile['program_templates'].items():
                if kind not in ('DI','DO','AI','AO'):
                    raise ValueError('命名规则的信号类型必须是 DI/DO/AI/AO')
                for _,field,spec,conversion in Formatter().parse(template):
                    if field is not None and (field not in ('station','module','channel','kind','direction')
                                              or conversion or not re.fullmatch(r'(?:0?[1-9][0-9]?d)?',spec)):
                        raise ValueError('程序模板仅允许 station/module/channel/kind/direction 字段及整数宽度格式')
            for regex in profile['patterns']:
                pattern=re.compile(regex)
                if not {'station','module','channel'} <= pattern.groupindex.keys():
                    raise ValueError('命名规则需要 station、module、channel 命名捕获组')
                if not ('kind' in pattern.groupindex or profile.get('kind') in ('DI','DO','AI','AO')):
                    raise ValueError('命名规则需要 kind 捕获组或固定类型')
                loaded.append((profile,pattern))
        signature=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    else:
        signature='builtin-v1'
    _CUSTOM, _RULES_SIGNATURE=loaded,signature


def rules_signature():return _RULES_SIGNATURE


@dataclass(frozen=True)
class Address:
    scheme: str
    kind: str
    station: str
    module: str
    channel: int
    raw: str
    terminal: str = ''
    template: str = ''

    @property
    def key(self):
        return f'{self.scheme}:{self.kind}:{self.station}:{self.module}:{self.channel}'

    @property
    def input(self):
        return self.kind in ('DI', 'AI')

    def to_dict(self):
        return {**asdict(self), 'key': self.key}

    def program_token(self):
        if self.template:
            return self.template.format(station=self.station,module=int(self.module) if self.module.isdigit() else self.module,
                                        channel=self.channel,kind=self.kind,
                                        direction={'DI':'I','DO':'Q','AI':'AI','AO':'AQ'}[self.kind])
        if self.scheme == 'station':
            if self.kind in ('DI', 'DO'):
                return f'{self.station}_{"I" if self.input else "Q"}{self.module}_{self.channel:02d}'
            return f'{self.station}_{self.module}{"AI" if self.input else "AQ"}{self.channel}'
        prefix = {'DI': 'ID', 'DO': 'OD', 'AI': 'AD', 'AO': 'DA'}[self.kind]
        ch = f'{self.channel:02d}' if self.kind in ('DI', 'DO') else str(self.channel)
        return f'{prefix}_{self.module}_{ch}'


_STATION = re.compile(r'(?P<station>[A-Z]+\d+)_(?:(?P<digital>[IQO])(?P<dm>\d+)_(?P<dc>\d{1,2})|(?P<am>\d+)(?P<analog>AI|AQ|AO)(?P<ac>\d+)(?P<terminal>I\+|V\+|\+|-)?)(?!\w)')
_LEGACY = re.compile(r'(?:(?P<prefix>ID|OD|AD|DA)_(?P<module>[A-Z](?:\d[0-9A-Z]|\d{3}))(?:_(?P<ch>\d{1,2})|\[(?P<array>\d+)\])|(?P<draw>AD|DA|[IOAD])[-_](?P<dmodule>[A-Z](?:\d[0-9A-Z]|\d{3}))-(?P<dch>\d{1,2})(?P<terminal>[+-])?)(?!\w)')


def parse_address(token):
    """No case-folding or station renaming: PLC identifiers must remain exact."""
    m = _STATION.fullmatch(token)
    if m:
        if m['digital']:
            kind = 'DI' if m['digital'] == 'I' else 'DO'
            module, channel = str(int(m['dm'])), int(m['dc'])
        else:
            kind = 'AI' if m['analog'] == 'AI' else 'AO'
            module, channel = str(int(m['am'])), int(m['ac'])
        return Address('station', kind, m['station'], module, channel, token, m['terminal'] or '')
    m = _LEGACY.fullmatch(token)
    if m:
        prefix = m['prefix'] or {'I':'ID', 'O':'OD', 'A':'AD', 'D':'DA','AD':'AD','DA':'DA'}[m['draw']]
        kind = {'ID':'DI', 'OD':'DO', 'AD':'AI', 'DA':'AO'}[prefix]
        return Address('legacy', kind, '', m['module'] or m['dmodule'],
                       int(m['ch'] or m['array'] or m['dch']), token, m['terminal'] or '')
    found=[]
    for profile,pattern in _CUSTOM:
        m=pattern.fullmatch(token)
        if not m:continue
        groups=m.groupdict()
        raw_kind=groups.get('kind') or profile.get('kind')
        kind=profile.get('kind_map',{}).get(raw_kind,raw_kind)
        if kind not in profile['program_templates']:
            continue
        module=groups['module']
        module=str(int(module)) if module.isdigit() else module
        if not groups['channel'].isdigit():continue
        found.append(Address('custom-'+profile['name'],kind,groups['station'],module,int(groups['channel']),token,
                             groups.get('terminal') or '',profile['program_templates'][kind]))
    # Overlapping rules must be resolved in the config, never by list order.
    if found and len({a.key for a in found})==1:
        return found[0]
    return None


def find_addresses(text):
    seen=set()
    for pattern in (_STATION, _LEGACY, *(p for _,p in _CUSTOM)):
        for m in pattern.finditer(text):
            if m.start() and (text[m.start()-1].isalnum() or text[m.start()-1] == '_'):
                continue
            if m.end()<len(text) and (text[m.end()].isalnum() or text[m.end()]=='_'):
                continue
            a = parse_address(m.group())
            identity=(m.start(),m.end(),a.key if a else None)
            if a and identity not in seen:
                seen.add(identity)
                yield m.start(), m.end(), a


def code_lines(lines):
    """Preserve columns while masking comments/strings; supports nested ST comments."""
    depth = 0
    for line in lines:
        chars, i, quote = list(line), 0, None
        while i < len(line):
            if depth:
                if line.startswith('(*', i):
                    chars[i:i+2] = '  '; depth += 1; i += 2
                elif line.startswith('*)', i):
                    chars[i:i+2] = '  '; depth -= 1; i += 2
                else:
                    chars[i] = ' '; i += 1
            elif quote:
                chars[i] = ' '
                if line[i] == '$' and i+1 < len(line):
                    chars[i+1] = ' '; i += 2
                elif line[i] == quote:
                    if i+1 < len(line) and line[i+1] == quote:
                        chars[i+1] = ' '; i += 2
                    else:
                        quote = None; i += 1
                else:
                    i += 1
            elif line.startswith('//', i):
                chars[i:] = ' ' * (len(line)-i); break
            elif line.startswith('(*', i):
                chars[i:i+2] = '  '; depth = 1; i += 2
            elif line[i] in ("'", '"'):
                quote = line[i]; chars[i] = ' '; i += 1
            else:
                i += 1
        yield ''.join(chars)


def parse_program(folder):
    """One-line assignments only; complex expressions remain verbatim for review."""
    from pathlib import Path
    rows, unparsed = [], []
    for path in sorted(Path(folder).glob('*.st.txt')):
        lines = path.read_text(encoding='utf-8').splitlines()
        for ln, (raw, code) in enumerate(zip(lines, code_lines(lines)), 1):
            if ':=' not in code:
                if list(find_addresses(code)):
                    unparsed.append({'section':path.name[:-7], 'line':ln, 'raw':raw,
                                     'reason':'地址出现在多行语句或其他逻辑中，保留原文待核对'})
                continue
            matches = list(find_addresses(code))
            if not matches:
                # Broad signature discovery prevents silent omission of new address layouts.
                if re.search(r'\b[A-Z]+\d+_[A-Z0-9]+(?:_\d+)?\b', code):
                    unparsed.append({'section':path.stem[:-3], 'line':ln, 'raw':raw,
                                     'reason':'可能存在未支持的地址格式，需确认'})
                continue
            split = code.index(':=')
            valid = [(s,e,a) for s,e,a in matches if (a.input and s > split) or (not a.input and e <= split)]
            for start, end, a in valid:
                variable = code[:split].strip() if a.input else code[split+2:].strip().rstrip(';').strip()
                simple = code.count(':=') == 1 and code.count(';') == 1
                if a.input:
                    simple &= bool(re.fullmatch(r'[\w\u4e00-\u9fff]+(?:\[\d+\])?', variable))
                else:
                    simple &= code[:split].strip() == raw[start:end]
                rows.append({'section':path.name[:-7], 'line':ln, 'raw':raw, 'code':code,
                             'address':a.to_dict(), 'var':variable, 'span':[start,end],
                             'simple':bool(simple), 'refs':len(matches)})
            if len(valid) != len(matches):
                unparsed.append({'section':path.name[:-7], 'line':ln, 'raw':raw,
                                 'reason':'地址位于不符合输入/输出方向的赋值侧'})
    return rows, unparsed
