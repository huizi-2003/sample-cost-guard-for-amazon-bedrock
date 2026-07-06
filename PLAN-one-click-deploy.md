# 改造计划：一键 CloudFormation 部署（去 deploy.sh 依赖）

## 目标

用户在 GitHub 看到项目后，直接在 AWS CloudFormation Console 导入 YAML URL 即可创建完整堆栈，无需本地环境、无需 AWS CLI、无需运行 deploy.sh。

## 当前问题

`deploy.sh` 做了 5 件事，其中 "打包代码上传 S3" 是 CFN 模板无法独立完成的：
1. 创建 S3 桶
2. **打包 lambda.zip 并上传 S3** ← 鸡蛋问题
3. 部署 CFN 栈
4. 更新 Lambda 代码
5. SSM 刷新 EC2

## 解决方案

GitHub Actions 在 Release 时打包 zip 上传为 Release asset；模板内用 inline Custom Resource Lambda 从 GitHub 下载 zip 到自建 S3 桶，解决鸡蛋问题。

---

## 修改清单

### 1. 新增 GitHub Actions Workflow

**文件**：`.github/workflows/release.yml`

**触发条件**：push tag `v*`

**步骤**：
```yaml
- checkout 代码
- 打包：zip -r lambda.zip common/ monitor/ reconciler/ web/ -x '*/__pycache__/*' '*.pyc'
- 创建 GitHub Release，上传 lambda.zip 为 asset
```

**Release asset URL 格式**（公开仓库可直接下载）：
```
https://github.com/{owner}/{repo}/releases/download/{tag}/lambda.zip
# latest 的固定地址：
https://github.com/{owner}/{repo}/releases/latest/download/lambda.zip
```

---

### 2. 改写 template.yaml

#### 2.1 新增参数

```yaml
Parameters:
  CodeVersion:
    Type: String
    Default: latest
    Description: >
      GitHub Release tag (e.g. v1.0.0) or "latest".
      Used to download lambda.zip from GitHub Releases.
  GitHubRepo:
    Type: String
    Default: "{owner}/sample-cost-guard-for-amazon-bedrock"
    Description: GitHub owner/repo for downloading release assets.
```

#### 2.2 新增资源：S3 桶（替代外部桶）

```yaml
CodeBucket:
  Type: AWS::S3::Bucket
  Properties:
    BucketName: !Sub "bedrock-lite-guard-${AWS::AccountId}-${AWS::Region}"
    LifecycleConfiguration:
      Rules:
        - Id: CleanupOldVersions
          Status: Enabled
          NoncurrentVersionExpiration:
            NoncurrentDays: 7
```

#### 2.3 新增资源：Bootstrap Lambda（inline，< 4KB）

用 `ZipFile` 内联一个 Python Lambda，职责：
1. 收到 CustomResource Create/Update 事件
2. 从 GitHub Release URL 下载 `lambda.zip`
3. 上传到 CodeBucket 的 `bedrock-lite-guard/lambda.zip`
4. 返回 SUCCESS 给 CloudFormation

```yaml
BootstrapFunction:
  Type: AWS::Lambda::Function
  Properties:
    FunctionName: !Sub "${AWS::StackName}-bootstrap"
    Runtime: python3.12
    Handler: index.handler
    Timeout: 120
    MemorySize: 256
    Role: !GetAtt BootstrapRole.Arn
    Code:
      ZipFile: |
        import json, urllib.request, boto3, cfnresponse

        def handler(event, context):
            try:
                if event['RequestType'] == 'Delete':
                    cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
                    return

                props = event['ResourceProperties']
                repo = props['GitHubRepo']
                version = props['CodeVersion']
                bucket = props['S3Bucket']
                s3_key = props['S3Key']

                if version == 'latest':
                    url = f"https://github.com/{repo}/releases/latest/download/lambda.zip"
                else:
                    url = f"https://github.com/{repo}/releases/download/{version}/lambda.zip"

                print(f"Downloading from {url}")
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()

                print(f"Uploading {len(data)} bytes to s3://{bucket}/{s3_key}")
                boto3.client('s3').put_object(Bucket=bucket, Key=s3_key, Body=data)

                cfnresponse.send(event, context, cfnresponse.SUCCESS,
                                 {'Bucket': bucket, 'Key': s3_key})
            except Exception as e:
                print(f"Error: {e}")
                cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
```

#### 2.4 新增资源：Bootstrap IAM Role

```yaml
BootstrapRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Principal:
            Service: lambda.amazonaws.com
          Action: sts:AssumeRole
    Policies:
      - PolicyName: BootstrapPolicy
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: s3:PutObject
              Resource: !Sub "arn:${AWS::Partition}:s3:::${CodeBucket}/*"
            - Effect: Allow
              Action:
                - logs:CreateLogGroup
                - logs:CreateLogStream
                - logs:PutLogEvents
              Resource: "*"
```

#### 2.5 新增资源：Custom Resource 触发 Bootstrap

```yaml
BootstrapTrigger:
  Type: AWS::CloudFormation::CustomResource
  DependsOn: CodeBucket
  Properties:
    ServiceToken: !GetAtt BootstrapFunction.Arn
    GitHubRepo: !Ref GitHubRepo
    CodeVersion: !Ref CodeVersion
    S3Bucket: !Ref CodeBucket
    S3Key: bedrock-lite-guard/lambda.zip
```

#### 2.6 修改现有 Lambda 资源

Monitor/Reconciler 的 `Code:` 改为引用自建桶：

```yaml
MonitorFunction:
  DependsOn: BootstrapTrigger  # 确保代码已下载
  Properties:
    Code:
      S3Bucket: !Ref CodeBucket
      S3Key: bedrock-lite-guard/lambda.zip
```

#### 2.7 修改 EC2 UserData

S3 地址改为引用 CodeBucket：
```bash
aws s3 cp s3://${CodeBucket}/bedrock-lite-guard/lambda.zip /tmp/lambda.zip --region ${AWS::Region}
```

#### 2.8 修改 EC2 IAM Policy

S3 GetObject 的 Resource 改为 CodeBucket：
```yaml
Resource: !Sub "arn:${AWS::Partition}:s3:::${CodeBucket}/*"
```

#### 2.9 删除原 S3Bucket 参数

`S3Bucket` 参数不再需要（桶由模板自建）。`S3Key` 参数可硬编码为常量。

---

### 3. 升级/更新机制

用户想升级到新版本时：
1. **Update Stack** → 修改 `CodeVersion` 参数为新 tag（如 `v1.1.0`）
2. CloudFormation 检测到 CustomResource Properties 变化 → 触发 Update → bootstrap 重新下载
3. Lambda Code S3Key 不变，需要额外触发代码更新 —— 两种方式：
   - **方式 A**：在 S3Key 里拼版本号（`lambda-{version}.zip`），CFN 自动检测变化并更新 Lambda
   - **方式 B**：在 BootstrapTrigger 返回时输出一个随机 hash，Lambda 引用这个 hash 作为 `S3ObjectVersion`

**推荐方式 A**：S3Key 改为 `bedrock-lite-guard/lambda-${CodeVersion}.zip`，每次版本变化 CFN 自动 rolling update Lambda 和 EC2。

对 `latest` 的处理：bootstrap Lambda 下载后，同时写两个 key：
- `bedrock-lite-guard/lambda-latest.zip`（给 CFN 引用）
- 实际上每次 Update 都会重新下载，所以 latest 也能更新

---

### 4. 更新 README

#### 新增部署方式（置顶）

```markdown
## 快速部署（推荐）

1. 点击下方按钮，或在 CloudFormation Console 选择"Create Stack" → "With new resources"
2. Template URL 填：`https://raw.githubusercontent.com/{owner}/{repo}/main/template.yaml`
3. 填写参数：
   - **AllowedCidr**（必填）：你的公网 IP，如 `1.2.3.4/32`
   - CodeVersion：默认 latest，也可指定版本如 `v1.0.0`
4. 勾选 "I acknowledge that AWS CloudFormation might create IAM resources"
5. Create Stack，等待 3-5 分钟
6. 在 Outputs 找到 WebPublicIp，访问 http://<IP>

### 更新到新版本

Update Stack → 修改 CodeVersion 为新版本号 → Update
```

#### 保留 deploy.sh 说明

```markdown
## 开发者本地部署（可选）

如果你 fork 了项目做二次开发，可继续使用 deploy.sh 本地部署...
```

---

### 5. 保留 deploy.sh（兼容）

deploy.sh 继续保留给开发者用。改造不破坏现有行为。

可选优化：deploy.sh 检测到模板已含 CodeBucket 资源时，直接上传到那个桶（而非自建额外桶）。

---

### 6. 可选：添加 "Launch Stack" 按钮

在 README 顶部加一个一键部署按钮（需要模板放在公开可访问的 URL）：

```markdown
[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/new?stackName=bedrock-lite-guard&templateURL=https://raw.githubusercontent.com/{owner}/{repo}/main/template.yaml)
```

> 注意：GitHub raw URL 有时被 CloudFormation 拒绝（CORS/redirect），备选方案是将 template.yaml 也放到公开 S3 桶。

---

## 风险与注意事项

| 风险 | 影响 | 缓解 |
|------|------|------|
| GitHub Release URL 被限流 | 创建栈超时 | bootstrap Lambda 加重试；lambda.zip 不大（< 1MB） |
| 用户网络不通 GitHub | 创建失败 | README 说明：中国区域需手动上传，走 deploy.sh |
| Custom Resource 失败不好调试 | 用户困惑 | bootstrap Lambda 日志写 CloudWatch；错误信息返回 CFN 事件 |
| CodeVersion=latest 时用户 Update Stack 但参数没变 | CFN 认为无变化不触发 | 文档说明：latest 需手动触发。或加一个 `ForceUpdate` 参数（随机值） |
| S3 桶名冲突（极低概率） | 创建失败 | 桶名含 AccountId + Region，基本不会冲突 |

---

## 实施顺序

1. 先写 GitHub Actions workflow，确认能正确打包并发布 Release
2. 改写 template.yaml（加 bootstrap 机制）
3. 本地测试：手动创建 Release → 用新模板创建栈 → 验证全流程
4. 更新 README
5. 打 v1.0.0 tag，正式发布

---

## 预计工作量

| 项目 | 预计时间 |
|------|---------|
| GitHub Actions workflow | 30 min |
| template.yaml 改造 | 1-2 hr（含测试） |
| README 更新 | 30 min |
| 端到端测试 | 1 hr |
| **合计** | ~3-4 hr |
