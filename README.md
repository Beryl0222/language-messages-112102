# 双语文化导览短句的替换词与语法交付

自助屏编校稿、设备变量约定和场次来宾请求。材料中的人、机构及经营记录是合成的。

每份资料为 UTF-8 JSON 数组。id 在同一份资料内唯一，原始文字按收到的形式保留。

- `data/copybook.json`。公众号自助屏编校交换记录。message_id 为设备使用的稳定业务编号。role_hint 是写稿人的备注。发布稿可能由旧程序录入。
- `data/screens.json`。设备方接口资料。required_keys 不改含义。max_columns 中等宽显示汉字占两列，组合附标和零宽连接符计为零；其余东亚宽度为 W 或 F 的字符计两列，其余计一列。waiting_ref 是批准过的独立退回消息。
- `data/classes.json`。活动方来宾请求。visit_class 是登记的可覆盖类型，data 是设备实际传入的值。第三条在新班次开始才允许。

使用 Python 3.11 标准库，无需安装软件包或连接外部服务。

以下命令分别读取一份原始资料、检查资料中的字段与关联。

```sh
python3 -m json.tool data/copybook.json
python3 -m unittest discover -s tests -v
```

## 本地工作台

`workbench/` 是给编校员用的只读工作台，用来在交付屏幕前核对改写和直译在
每类来宾身上会怎样。它只读 `data/`，不连接外部服务。

```sh
python3 -m workbench templates      # 逐条标注每个变量说的是谁、被谁修饰
python3 -m workbench classes        # 用四类合成请求预览中英文短句
python3 -m workbench --shift 晚班 classes
python3 -m workbench audit          # 成对语言的覆盖审核
python3 -m workbench preview        # 旧交付 vs 当前修订的逐条对照
python3 -m workbench report         # 交付总表：待补译、不可交付、通知设备

# 编校员自带合成请求（与 classes.json 同结构）：
python3 -m workbench classes --requests my-requests.json
# 加 --json 可存档或做回归比对。
```

### 判断规则

- **变量角色**：受话人取主句主语，称呼和地点只是修饰/旁语。英文
  `{guide}, please join …` 会被标为“领队当受话人”，与中文
  “来宾随领队走”的角色对调；`primary`、`guide`、`room`、`group_count`
  各自只按 `screens.json` 的固定含义解释。
- **量化和范围**：`group_count` 约定不含被领队者本人。`your party of N`
  若把本人算进去，对每一条请求都报错；`一行` 的二读交编辑选定，并列出
  它仍可服务的来宾类型（有成组同行者），单人请求降为待编辑。
- **词位是显示还是决定动作**：`honorific` 只修饰 `primary`，不改动作；
  它需要本人明确不拒绝才显示，未录称呼或本人拒绝时走中性写法。
- **一族写法**：单复数、冠词、有无身份称呼按值分开。英文 `(s)` 和
  `a {primary}` 这类拼接会被标出，不能靠一处替换覆盖所有情形。单词数
  相同、变量齐全都不作为译法保住集合归属的证据。
- **宽度**：按 `screens.json` 的列宽约定逐条计算展开后的文本；放不下时
  整条请求退回 `waiting_ref` 的独立文案，绝不截断姓名，也不会把兜底文案
  里没有的地点当作已授权目的地。
- **语言一致**：同一请求两种语言必须得到同一动作；一种语言只能兜底时，
  另一种也一起兜底。选择的顺序固定（条件越具体越优先，其次修订号，
  再次问题数，最后 id），任一实际请求都能确定唯一一条。
- **成对提交**：`audit` 比较两种语言的适用范围、称呼要求、动作和“其余
  来宾”的去向；一边扩大人群、另一边继续排除会被判为不可接受，并列出
  每种语言各自排除的来宾类别。只有一种语言有条件写法时记为待补译。
