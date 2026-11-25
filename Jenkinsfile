// Declarative pipeline: each stage calls ci.py, the same script GitHub Actions runs.
// Agent needs Python 3.11+, Docker, kubectl and k3d (Linux or Windows agent).

def ci(String stages) {
  if (isUnix()) { sh "${env.PY} ci.py ${stages}" } else { bat "${env.PY} ci.py ${stages}" }
}

pipeline {
  agent any
  options {
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }
  parameters {
    booleanParam(name: 'DEPLOY_K8S', defaultValue: true, description: 'Deploy to the local k3d cluster and test there')
  }
  stages {
    stage('Setup') {
      steps {
        script {
          env.PY = isUnix() ? '.venv/bin/python' : '.venv\\Scripts\\python.exe'
          if (isUnix()) {
            sh 'python3 -m venv .venv && .venv/bin/python -m pip install -q -e ".[dev]"'
          } else {
            bat 'python -m venv .venv && .venv\\Scripts\\python.exe -m pip install -q -e ".[dev]"'
          }
        }
      }
    }
    stage('Lint + tests')   { steps { script { ci('lint unit') } } }
    stage('Image + scan')   { steps { script { ci('build scan') } } }
    stage('Smoke test')     { steps { script { ci('smoke') } } }
    stage('Kubernetes') {
      when { expression { params.DEPLOY_K8S } }
      steps { script { ci('k8s') } }
    }
  }
  post {
    always { junit allowEmptyResults: true, testResults: 'reports/*.xml' }
  }
}
