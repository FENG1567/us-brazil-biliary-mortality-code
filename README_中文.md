# 美巴胆道疾病死亡比较：公开代码仓库

公开仓库：https://github.com/FENG1567/us-brazil-biliary-mortality-code

本代码版包含完整候选包对应的分析与绘图代码、可移植修订入口、合成数据测试、环境说明及来源/字段元数据。原创代码和文档采用MIT许可；第三方数据不因MIT而重新授权。

本次没有公开研究结果CSV、个体死亡记录、人口输入CSV、图表PDF或精细联合模式数据。`docs/`中的四个CSV是元数据目录，不是研究数据。许可待确认的汇总数据继续保留在本地，未修改数值或以零替代小计数。

本仓库独立于旧的`us-brazil-biliary-death-certificate-representation`仓库；未覆盖旧仓库，也未重新认定旧版数据发布的许可状态。

运行测试请使用Python 3.12环境，命令见英文README。测试通过说明合成/可移植代码路径通过检查，不等于已重新运行论文分析。实际重建结果需要按`docs/EXTERNAL_INPUTS.md`和`docs/DATA_ACQUISITION_CONTRACT.md`准备精确外部输入；缺少人口数据的集成测试会明确跳过。

历史源代码中的部分默认路径保留原样，以维护哈希绑定身份；运行时按文档指定路径或使用可移植适配器。没有包含账号密码、私钥、原始记录或服务器控制器。

可直接用于论文的英文代码可用性声明见`docs/CODE_AVAILABILITY.md`。本次不生成或虚构Zenodo DOI、论文DOI或完整数据公开许可。
