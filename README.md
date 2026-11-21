# 双语文化导览短句的替换词与语法交付

自助屏编校稿、设备变量约定和场次来宾请求。材料中的人、机构及经营记录是合成的。

每份资料为 UTF-8 JSON 数组。id 在同一份资料内唯一，原始文字按收到的形式保留。

- `data/copybook.json`。公众号自助屏编校交换记录。message_id 为设备使用的稳定业务编号。role_hint 是写稿人的备注。发布稿可能由旧程序录入。
- `data/screens.json`。设备方接口资料。required_keys 不改含义。max_columns 中等宽显示汉字占两列，组合附标和零宽连接符计为零；其余字符按说明中的宽度。waiting_ref 是批准过的独立退回消息。
- `data/classes.json`。活动方来宾请求。visit_class 是登记的可覆盖类型，data 是设备实际传入的值。第三条在新班次开始才允许。

使用 Python 3.11 标准库，无需安装软件包或连接外部服务。

以下命令分别读取一份原始资料、检查资料中的字段与关联。

```sh
python3 -m json.tool data/copybook.json
python3 -m unittest discover -s tests -v
```
