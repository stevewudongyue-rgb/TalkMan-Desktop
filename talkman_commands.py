"""应用指令白名单。只解析完整口令，不执行模型生成的代码或任意设置字段。"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Command:
    action: str
    value: object = None


OPTIONS = {
    '去语气词': ('auto_filler', 'filler_var'),
    '去客气表达': ('direct_request', 'direct_request_var'),
    '说话人角色': ('speaker_enabled', 'speaker_enabled_var'),
    '自动标点': ('auto_punct', 'punct_var'),
    'AI分行排版': ('auto_layout', 'layout_var'),
    'AI整理语序': ('auto_rewrite', 'rewrite_var'),
    '改写预览': ('preview_ai', 'preview_ai_var'),
    '只显示Agent': ('only_agents', 'only_agents_var'),
    '填入后清空': ('clear_after_fill', 'clear_after_fill_var'),
    '鼠标滚轮移动光标': ('wheel_caret', 'wheel_caret_var'),
    '文字放大镜': ('magnifier', 'magnifier_var'),
    '窗口置顶': ('topmost', 'topmost_var'),
    '自动收起': ('edge_hide', 'edge_hide_var'),
    '全局快捷键': ('global_voice', 'global_voice_var'),
    '免费服务': ('free_mode', 'free_mode_var'),
    '同步模式': ('sync_mode', 'sync_mode_var'),
    '启动检查更新': ('check_updates', 'update_auto'),
}
ALIASES = {
    '自动去语气词': '去语气词', '语气词': '去语气词', '标点': '自动标点',
    '去客气': '去客气表达', '姓名代入': '说话人角色', '第三人称录入': '说话人角色',
    '分行排版': 'AI分行排版', '自动排版': 'AI分行排版', 'AI排版': 'AI分行排版',
    '整理语序': 'AI整理语序', '自动改写': 'AI整理语序', 'AI改写': 'AI整理语序',
    '结果预览': '改写预览', 'AI预览': '改写预览', 'AI和指令结果预览': '改写预览',
    '只显示Agent对象': '只显示Agent', '仅显示Agent': '只显示Agent',
    '下发后清空': '填入后清空', '自动清空': '填入后清空',
    '放大镜': '文字放大镜', '置顶': '窗口置顶', '贴边收起': '自动收起',
    '后台快捷键': '全局快捷键', '免费模式': '免费服务', '实时同步': '同步模式',
}
PAGES = {'设置':'text', '指令设置':'text', '指令改写设置':'text', 'AI设置':'text',
         '语音识别模型':'asr', '语音设置':'asr', '文字改写模型':'model',
         '模型设置':'model', '快捷键设置':'controls', '窗口设置':'window',
         '目标设置':'target', '约定词设置':'conventions', '版本信息':'about'}
SHORTCUT_NAMES = {'录音':'replace', '录音和覆盖':'replace', '直接覆盖':'replace',
    '指令':'rewrite', '指令修改':'rewrite', 'Command':'rewrite', '麦克风':'mic',
    '清空':'clear', '清空编辑区':'clear', '聚焦':'focus', '聚焦鼠标位置':'focus',
    '光标前移':'cursor_prev', '光标后移':'cursor_next', '全局录音':'global_record',
    '全局录音和覆盖':'global_record', '全局指令':'global_rewrite', '全局指令修改':'global_rewrite',
    '唤醒':'global_show', '唤醒TalkMan':'global_show'}


def compact(text):
    return re.sub(r'\s+', '', text).casefold()


def named(text, mapping):
    return next((v for k, v in mapping.items() if compact(k) == compact(text)), None)


def parse_command(text):
    """None 表示普通正文改写。未知应用口令返回 unsupported，绝不写进正文。"""
    if not isinstance(text, str) or len(text) > 2000:
        return None
    raw = text.strip().rstrip('。.!！?？')
    raw = re.sub(r'^(?:Talk\s*Man[，,：:\s]*|请(?:帮我)?|帮我)', '', raw, flags=re.I).strip()
    phrase = compact(raw)
    direct = {
        '保存到草稿簿':'save', '保存草稿':'save', '存为草稿':'save', '记录到草稿':'save',
        '记录到草稿簿':'save', '记录到历史':'manual_history', '保存到历史':'manual_history',
        '打开草稿簿':'drafts', '打开草稿':'drafts', '打开历史':'history', '查看历史':'history',
        '关闭记录':'close_records', '关闭草稿簿':'close_records', '关闭历史':'close_records',
        '复制全部':'copy', '复制全部文本':'copy', '撤销':'undo', '上一步':'undo',
        '重做':'redo', '下一步':'redo', '全选':'select_all', '全选正文':'select_all',
        '取消选区':'deselect', '清空编辑区':'clear', '清空全部文字':'clear',
        'AI整理一次':'organize', '整理一次':'organize', '翻译选中文字':'translate',
        '翻译选区':'translate', '填入Agent':'fill', '填入目标':'fill',
        '下发':'send', '发送给Agent':'send', '确认执行':'confirm', '确认操作':'confirm', '确认':'confirm',
        '确认恢复':'confirm', '确认替换':'confirm', '确认保存':'confirm',
        '确认下发':'confirm', '保存约定':'confirm', '取消':'cancel',
        '取消操作':'cancel', '取消指令':'cancel', '关闭设置':'close_settings',
        '指令帮助':'help', '可以说什么':'help', '展开窗口':'expand',
    }
    action = named(raw, direct)
    if action:
        return Command(action)
    for name, page in PAGES.items():
        if phrase in (compact('打开'+name), compact('进入'+name)):
            return Command('settings', page)
    m = re.fullmatch(r'(?:把|将)?(?:上一条|最近一条|最新一条)(历史|草稿|草稿簿)(?:放入编辑框|放回编辑框|恢复到编辑框|恢复|调出来)', raw)
    if m:
        return Command('restore', 'delivery' if m[1] == '历史' else 'notebook')
    m = re.fullmatch(r'(?:恢复|调出)(?:上一条|最近一条|最新一条)(历史|草稿|草稿簿)', raw)
    if m:
        return Command('restore', 'delivery' if m[1] == '历史' else 'notebook')
    m = re.fullmatch(r'(打开|开启|启用|关闭|关掉|停用)(.+?)(?:开关)?', raw)
    if not m:
        m2 = re.fullmatch(r'(?:把|将)(.+?)(?:开关)?(打开|开启|启用|关闭|关掉|停用)', raw)
        if m2:
            verb, option = m2[2], m2[1]
        else:
            verb = option = None
    else:
        verb, option = m[1], m[2]
    if option:
        label = named(option, {name:name for name in OPTIONS}) or named(option, ALIASES)
        if label:
            return Command('option', (label, verb in ('打开', '开启', '启用')))
    m = re.fullmatch(r'(?:把|将)?(.+?)(?:的)?快捷键(?:设置成|设置为|设为|改成|改为)(.+)', raw)
    if m:
        action = named(m[1], SHORTCUT_NAMES)
        if action:
            key = re.sub(r'\s+', '', m[2].strip().rstrip('。'))
            key = re.sub('加号|加|和', '+', key).replace('键', '')
            return Command('shortcut', (action, key))
        return Command('unsupported', '未识别快捷键对应的功能，请说“把指令快捷键改成 Ctrl 加 Alt 加 J”。')
    m = re.fullmatch(r'(?:把|将)?(?:自动整理|AI整理)(?:的)?延迟(?:设置成|设置为|设为|改成|改为)(\d+)秒', raw, re.I)
    if m:
        return Command('delay', int(m[1]))
    m = re.fullmatch(r'(?:把|将)?翻译(?:的)?目标语言(?:设置成|设置为|设为|改成|改为)(.+)', raw)
    if m:
        return Command('language', m[1].strip())
    m = re.fullmatch(r'(?:把|将)?(?:说话人|角色)(?:的)?(?:名字|姓名)(?:设置成|设置为|设为|改成|改为)(.+)', raw)
    if m:
        return Command('speaker_name', m[1].strip())
    m = re.fullmatch(r'(?:新增|记住|添加)(?:一个)?约定(?:词)?[，,：:\s]*(.+?)[，,；;\s]*(?:不是|不要写成|而不是)[，,：:\s]*(.+)', raw)
    if m:
        clean = lambda s: s.strip(' ，,：:；;“”\"「」')
        return Command('convention', (clean(m[1]), clean(m[2])))
    m = re.fullmatch(r'(?:以后)?把[“「\"]?(.+?)[”」\"]?(?:记为|约定为)[“「\"]?(.+?)[”」\"]?', raw)
    if m:
        return Command('convention', (m[2].strip(), m[1].strip()))
    m = re.fullmatch(r'(?:记住|添加|新增)优先词[，,：:\s]*(.+)', raw)
    if m:
        return Command('priority_word', m[1].strip(' ，,：:“”「」'))
    if re.fullmatch(r'(?:切换到|使用)(深色|浅色|瑞士国际|瑞士)(?:模式|主题|风格)?', raw):
        return Command('theme', 'swiss' if '瑞士' in raw else 'dark' if '深色' in raw else 'light')
    m = re.fullmatch(r'(?:选择|切换到)(?:目标|Agent|agent)\s*(.+)', raw)
    if m:
        return Command('target', m[1].strip())
    if re.fullmatch(r'(?:收起到|收起在)(左边|右边|顶边|顶部)', raw):
        return Command('dock', 'left' if '左' in raw else 'right' if '右' in raw else 'top')
    m = re.fullmatch(r'(?:把|将)?录音(?:方式|模式)(?:设置成|设置为|设为|改成|改为)(按住|点按|两种都支持|按住和点按)', raw)
    if m:
        return Command('gesture', {'按住':'hold','点按':'toggle'}.get(m[1], 'both'))
    m = re.fullmatch(r'(?:把|将)?(?:文字大小|界面字号)(?:设置成|设置为|设为|改成|改为)(100%|125%|150%|标准|大号|最大)', raw)
    if m:
        return Command('scale', {'100%':1.0,'标准':1.0,'125%':1.25,'大号':1.25,'150%':1.5,'最大':1.5}[m[1]])
    m = re.fullmatch(r'(?:切换到|使用)(Python|JavaScript|TypeScript|SQL|普通)(?:代码)?口述', raw, re.I)
    if m:
        return Command('code_language', {'python':'python','javascript':'javascript','typescript':'javascript','sql':'sql','普通':'off'}[m[1].casefold()])
    if re.match(r'(?:设置|修改|把|将).*(?:API\s*Key|API密钥|账号密码)', raw, re.I):
        return Command('unsupported', '密钥请在模型设置中填写；不会把这段口述交给文字模型或写入正文。')
    if phrase.startswith(('打开', '关闭', '启用', '停用', '新增约定', '记住约定')) or '快捷键' in phrase:
        return Command('unsupported', '没认出这个应用操作。可说“指令帮助”查看例句；改正文请说“把这句话改成……” 。')
    return None
