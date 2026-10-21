pipeline {
  agent { label 'generic' }

  options {
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  stages {
    stage('betterleaks') {
      steps {
        withChecks('betterleaks') {
          sh 'betterleaks git . --redact --no-banner --verbose'
        }
      }
    }

    stage('tests') {
      steps {
        withChecks('tests') {
          sh '''
            status=0
            for suite in scripts/tests/*.sh; do
              "$suite" || status=1
            done
            exit "$status"
          '''
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
