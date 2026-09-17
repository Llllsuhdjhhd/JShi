# 冒烟测试产物（smoke）

本目录保留手工冒烟测试生成的数据，便于直接查看 01 身份识别的实际落库结果。

## 目录

```text
smoke/
  README.md
  01-身份识别/
    identities.json      # 匠石身份
    subject.sqlite3      # 对象档案 + 事实/主体历史（object_profiles 表）
```

## 复现命令

在项目根目录（Anaconda Prompt）：

```powershell
$env:PYTHONPATH="src"
$dir="tests/smoke/01-身份识别"

python -m jshi.app.cli --data-dir $dir create stone
python -m jshi.app.cli --data-dir $dir add-object 张三 --aliases 阿三
python -m jshi.app.cli --data-dir $dir add-object 声纹好友 --carrier voiceprint:vp-9
python -m jshi.app.cli --data-dir $dir experience stone "你好，老朋友" --speaker 张三
python -m jshi.app.cli --data-dir $dir experience stone "你好" --carrier voiceprint:vp-9   # 模式一：载体命中 → 直接给对象名
python -m jshi.app.cli --data-dir $dir preview-state stone "继续" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "又见面了" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "你好" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "设备问候" --channel dev-7       # 渠道引用未匹配 → 新建暂定对象（0.70）
python -m jshi.app.cli --data-dir $dir experience stone "你好"                           # 预期报错：invalid input envelope（无对象引用）
```

## 查看

```powershell
python -m jshi.app.cli --data-dir $dir history stone --kind subject --limit 20
python -m jshi.app.cli --data-dir $dir history stone --kind fact --limit 20

# 直接查对象档案表
python -c "import sqlite3; c=sqlite3.connect(r'tests/smoke/01-身份识别/subject.sqlite3'); print(list(c.execute('select object_id,label,source,status from object_profiles')))"
```

说明：该库是生成快照，schema 或行为变更后可删除重建。
