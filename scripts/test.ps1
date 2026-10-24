$ErrorActionPreference = "Stop"
$databaseName = "wallet_test_$([guid]::NewGuid().ToString('N'))"
$databaseCreated = $false

try {
    docker compose up -d --wait db
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL failed to start."
    }

    docker compose exec -T db psql -U wallet -d postgres -v ON_ERROR_STOP=1 `
        -c "CREATE DATABASE $databaseName"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the disposable test database."
    }
    $databaseCreated = $true

    Get-Content -Raw .\migrations\001_init.sql |
        docker compose exec -T db psql -U wallet -d $databaseName -v ON_ERROR_STOP=1
    if ($LASTEXITCODE -ne 0) {
        throw "Migration 001 failed."
    }

    Get-Content -Raw .\migrations\002_fee_and_hold_accounts.sql |
        docker compose exec -T db psql -U wallet -d $databaseName -v ON_ERROR_STOP=1
    if ($LASTEXITCODE -ne 0) {
        throw "Migration 002 failed."
    }

    $repoMount = "$($PWD.Path):/app"
    $testDatabaseUrl = "postgresql://wallet:wallet@db:5432/$databaseName"
    docker compose run --rm --no-deps -v $repoMount `
        -e "TEST_DATABASE_URL=$testDatabaseUrl" api `
        python -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "The test suite failed."
    }
}
finally {
    if ($databaseCreated) {
        docker compose exec -T db psql -U wallet -d postgres -v ON_ERROR_STOP=1 `
            -c "DROP DATABASE $databaseName WITH (FORCE)"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Could not remove disposable test database $databaseName."
        }
    }
}
