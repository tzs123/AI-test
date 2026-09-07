param(
    [Parameter(Mandatory = $false)]
    [string]$RunnerGoHost = "192.168.0.92"
)

$ErrorActionPreference = "Stop"
$checks = @(
    @{ Name = "RunnerGo 前端"; Port = 9999; Url = "http://$RunnerGoHost`:9999/health" },
    @{ Name = "RunnerGo 后端"; Port = 8000; Url = "http://$RunnerGoHost`:8000/api/health" }
)

foreach ($check in $checks) {
    $tcp = Test-NetConnection -ComputerName $RunnerGoHost -Port $check.Port -WarningAction SilentlyContinue
    if (-not $tcp.TcpTestSucceeded) {
        throw "$($check.Name) TCP $($check.Port) 不可达。请检查虚拟机网络、防火墙和 Docker 端口映射。"
    }

    try {
        $response = Invoke-WebRequest -Uri $check.Url -UseBasicParsing -TimeoutSec 10
        Write-Host "通过: $($check.Name) [$($response.StatusCode)] $($check.Url)" -ForegroundColor Green
    }
    catch {
        throw "$($check.Name) HTTP 检查失败: $($_.Exception.Message)"
    }
}

Write-Host "RunnerGo 连接检查完成。前端: http://$RunnerGoHost`:9999" -ForegroundColor Cyan
