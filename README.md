# 双语文化导览短句的替换词与语法交付

自助屏编校稿、设备变量约定和场次来宾请求。材料中的人、机构及经营记录是合成的。

每份资料为 UTF-8 JSON 数组。id 在同一份资料内唯一，原始文字按收到的形式保留。

- `data/copybook.json`。公众号自助屏编校交换记录。message_id 为设备使用的稳定业务编号。role_hint 是写稿人的备注。发布稿可能由旧程序录入。
- `data/screens.json`。设备方接口资料。required_keys 不改含义。max_columns 中等宽显示汉字占两列，组合附标和零宽连接符计为零；其余东亚宽度为 W 或 F 的字符计两列，其余计一列。waiting_ref 是批准过的独立退回消息。
- `data/classes.json`。活动方来宾请求。visit_class 是登记的可覆盖类型，data 是设备实际传入的值。第三条在新班次开始才允许。

使用 Python 3.11 标准库，无需安装软件包或连接外部服务。

以下命令逐层核对资料、短句和屏幕交付。

```sh
python3 -m json.tool data/copybook.json
python3 -m unittest discover -s tests -v

python3 -m guideworkbench review            # 每条短句的角色、集合、范围与待编辑项
python3 -m guideworkbench pairs guide-7     # 列出全部中英组合及适用范围
python3 -m guideworkbench screen S1         # 一台屏幕下各类来宾分别会看到什么
python3 -m guideworkbench survey            # 按访客类别合成请求逐一预览
python3 -m guideworkbench request R2        # 一条实际请求的屏幕预览
python3 -m guideworkbench request R3 --shift 晚班
python3 -m guideworkbench intake            # 现有请求能否照常预览、缺口与需通知的设备
```

## 工作台怎么读短句

`guideworkbench` 不把“变量名齐全、字数一样”当成译法正确。每条已登记短句都带一份读法声明：

- **变量指谁**：`{guide}` 是实际领队、随行到目的地，不是被要求去会合的受话人；`{primary}` 是本条消息要引领的来宾。角色被对调时（如 V2）直接拦下。
- **谁被限定**：“同组的”把同行者限定为与主来宾同一行的人；`group_count` 的字段约定是不含本人，读成整团人数（V3）会把一人留在原处。
- **第二句指谁**：“其余来宾 / remaining guests”是第一句同行集合在来宾范围内的补集；“Everyone else / Other guests / 其他人”是另一批人或所有人的补集，范围不同，只能作为待编辑项。
- **词位的用途**：一个变量可以只显示（`display`）、只决定分流（`control`），或两者兼有。V8 的 `group_count` 只用于判断是否单独来宾，不显示；V9 的 `honorific` 既显示又受本人同意约束。
- **有多窄**：单独来宾、成组来宾、有经本人同意的身份称呼是不同范围。两个短写法各覆盖一部分来宾可以并存，但任一实际请求只落进一个范围。

没有已认可读法的短句不会被推测成“同样适用”：它进入待编辑列表，并列出仍可服务的来宾类型。

## 英文形态与称呼

- `(s)`、`(guest(s))` 这类括号兼写不能由一次替换覆盖 0、1、多人；这类短句会被标为需按人数另写。
- 没有记录称呼、本人也不愿被称先生/女士时，只有**获准的中性写法**（`RenderPolicy.neutral_honorific_approved`）才可通过；不能用空字符串顶替后继续扩大人群。
- 中文有称呼词位、英文没有（或相反）的句对，切换语言会丢失或凭空增加称呼，不能作为同一覆盖提交。

## 交付规则

1. 一条实际请求只在**两种语言共同且唯一**的覆盖范围内走路线指引。
2. 范围签署（`Approval`）只能确认“另一批人/其余”是否指同一剩余来宾群体；不能放行人数读法、覆盖人群、称呼词位、英文形态或宽度问题。
3. 放不下时先给 `waiting_ref` 的独立静态文案（`guide-8`）。它必须不含姓名、领队、地点或人数，且两侧都能显示；缺一侧就不交付任何路线。
4. 正式提交（`assemble_copybook`）只接受同一语义覆盖的中英组合。一边热换成更宽的人群、另一边继续排除某类来宾的组合会被拦下。
5. 未到班次的请求（`R3` 未带 `--shift 晚班`）标记为 `not_yet`，不产出预览。
6. 只显示自选名的请求使用 `permitted_public_name`；设备传入的原名不会出现在屏幕或备用提示中。

## 库接口

```python
from guideworkbench import (
    load_workbench, resolve_delivery, CopyRequest,
    review_copybook, assemble_copybook, inspect_copybook,
    survey_classes, plan_screen, cross_screen_view,
    Approval, Scope, Semantics, make_route_semantic,
)

workbench = load_workbench()
delivery = resolve_delivery(workbench, CopyRequest.from_visitor_request(workbench.request("R2")))
print(delivery.status, delivery.action)
print(delivery.messages["zh"].text)
print(delivery.drafts["zh"].text)      # 需编辑时，草稿供核对但不显示在屏幕上
```

新增已认可的短句时，用 `make_route_semantic(scope=...)` 登记读法，再包成 `Semantics` 传入；不修改 `data/`。新增访客类别用 `inspect_copybook(extra_requests=[...])` 合成请求，看它是否会被现有文案误覆盖、以及需要通知哪台设备。
