"""Local named presets. Loading only changes the editor preview."""
from datetime import datetime
import json
from pathlib import Path
import uuid
from editor_state import read_document
from user_data import atomic_json


class PresetLibrary:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('预设库文件过大')
        items = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(items, list):
            raise ValueError('预设库格式错误')
        for item in items:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('id', 'name', 'category')):
                raise ValueError('预设条目格式错误')
            item['document'] = read_document(item.get('document'))
        return items

    def save(self, name, category, document):
        if not name.strip():
            raise ValueError('请填写预设名称')
        items = self.load()
        item = {'id': uuid.uuid4().hex, 'name': name.strip()[:100], 'category': category.strip()[:50],
                'favorite': False, 'created': datetime.now().astimezone().isoformat(),
                'document': read_document(document)}
        items.append(item)
        atomic_json(self.path, items)
        return item

    def update(self, identity, **changes):
        items = self.load()
        item = next(i for i in items if i['id'] == identity)
        for key in ('name', 'category', 'favorite'):
            if key in changes:
                item[key] = changes[key]
        if not str(item['name']).strip():
            raise ValueError('名称不能为空')
        atomic_json(self.path, items)

    def delete(self, identity):
        items = self.load()
        atomic_json(self.path, [i for i in items if i['id'] != identity])

    def search(self, query='', category='', favorites=False):
        return [i for i in self.load() if
                query.casefold() in (i['name']+' '+i['category']).casefold()
                and (not category or i['category'] == category)
                and (not favorites or i.get('favorite', False))]
