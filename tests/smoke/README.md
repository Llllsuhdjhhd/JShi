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
python -m jshi.app.cli --data-dir $dir experience stone "你好，老朋友" --speaker 张三
python -m jshi.app.cli --data-dir $dir preview-state stone "继续" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "又见面了" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "你好" --speaker 李四
python -m jshi.app.cli --data-dir $dir experience stone "你好" --channel unknown-device  # 预期报错，留下 object_rejected 记录
```

## 查看

```powershell
python -m jshi.app.cli --data-dir $dir history stone --kind subject --limit 20
python -m jshi.app.cli --data-dir $dir history stone --kind fact --limit 20

# 直接查对象档案表
python -c "import sqlite3; c=sqlite3.connect(r'tests/smoke/01-身份识别/subject.sqlite3'); print(list(c.execute('select object_id,label,source,status from object_profiles')))"
```

说明：该库是生成快照，schema 或行为变更后可删除重建。
