import importlib.util
import logging
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))
sys.path.append(str(DOCS_ROOT / 'extensions'))


def _install_native_voice_docs_stub() -> None:
    name = 'discord.ext.native_voice._native_voice'
    path = DOCS_ROOT / '_stubs' / 'native_module.py'
    spec = importlib.util.spec_from_file_location('_native_voice_docs_stub', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'could not load native voice docs stub from {path}')

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_native_voice_stub(name, ROOT / 'discord' / 'ext' / 'native_voice' / '_native_voice.pyi')


_install_native_voice_docs_stub()


extensions = [
    'builder',
    'sphinx.ext.autodoc',
    'sphinx.ext.extlinks',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinxcontrib_trio',
    'details',
    'exception_hierarchy',
    'attributetable',
    'resourcelinks',
]

autodoc_member_order = 'bysource'
autodoc_typehints = 'none'

extlinks = {
    'issue': ('https://github.com/dolfies/discord.py-self/issues/%s', 'GH-%s'),
}

intersphinx_mapping = {
    'py': ('https://docs.python.org/3', None),
    'discordpy_self': ('https://discordpy-self.readthedocs.io/en/latest/', None),
}

rst_prolog = """
.. |coro| replace:: This function is a |coroutine_link|_.
.. |maybecoro| replace:: This function *could be a* |coroutine_link|_.
.. |coroutine_link| replace:: *coroutine*
.. _coroutine_link: https://docs.python.org/3/library/asyncio-task.html#coroutine
"""

templates_path = ['_templates']
source_suffix = '.rst'
master_doc = 'index'

project = 'discord-native-voice'
copyright = '2026-present, Dolfies'

version = ''
with open(ROOT / 'pyproject.toml', encoding='utf-8') as fp:
    version_match = re.search(r'^version\s*=\s*[\'"]([^\'"]*)[\'"]', fp.read(), re.MULTILINE)
    if version_match is not None:
        version = version_match.group(1)

release = version
branch = 'master' if version.endswith('a') else 'v' + version
language = None
gettext_compact = False
exclude_patterns = ['_build']
pygments_style = 'friendly'

def _i18n_warning_filter(record: logging.LogRecord) -> bool:
    return not record.msg.startswith(
        (
            'inconsistent references in translated message',
            'inconsistent term references in translated message',
        )
    )


_i18n_logger = logging.getLogger('sphinx')
_i18n_logger.addFilter(_i18n_warning_filter)

html_experimental_html5_writer = True
html_theme = 'basic'
html_context = {
    'discord_extensions': [],
    'project_root_label': 'discord-native-voice',
    'project_github_url': 'https://github.com/dolfies/discord-native-voice',
    'documentation_root_label': 'discord.ext.native_voice',
}

resource_links = {
    'discordpy-self': 'https://github.com/dolfies/discord.py-self',
    'issues': 'https://github.com/dolfies/discord.py-self/issues',
    'repository': f'https://github.com/dolfies/discord-native-voice/tree/{branch}',
}

html_favicon = './images/discord_py_logo.ico'
html_static_path = ['_static']
html_search_scorer = os.path.join(os.path.dirname(__file__), '_static/scorer.js')
html_js_files = ['custom.js', 'settings.js', 'copy.js', 'sidebar.js']
htmlhelp_basename = 'discord.native.voice.doc'
html_show_sphinx = False

latex_documents = [
    ('index', 'discord-native-voice.tex', 'discord-native-voice Documentation', 'Dolfies', 'manual'),
]

man_pages = [('index', 'discord-native-voice', 'discord-native-voice Documentation', ['Dolfies'], 1)]

texinfo_documents = [
    (
        'index',
        'discord-native-voice',
        'discord-native-voice Documentation',
        'Dolfies',
        'discord-native-voice',
        'Native voice extension for discord.py-self.',
        'Miscellaneous',
    ),
]


def setup(app):
    pass
