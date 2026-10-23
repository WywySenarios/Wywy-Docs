pipeline {
  agent { label 'generic' }

  options {
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  stages {
    stage('betterleaks') {
      agent { label 'betterleaks' }
      steps {
        withChecks('betterleaks') {
          sh 'betterleaks git . --redact --no-banner --verbose'
        }
      }
    }

    stage('tests') {
      steps {
        withChecks('tests') {
          sh 'scripts/tests/unit.sh'
        }
      }
    }
  }

  post {
    always {
      cleanWs()
    }
  }
}
