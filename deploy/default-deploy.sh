./deploy.py create default --matrix synapse --domain default.mindroom.chat
./deploy.py create alt --matrix tuwunel --domain alt.mindroom.chat
./deploy.py create test --matrix tuwunel --domain test.mindroom.chat
./deploy.py create test-2 --matrix tuwunel --domain test-2.mindroom.chat
./deploy.py create test-3 --matrix tuwunel --domain test-3.mindroom.chat
./deploy.py create test-4 --matrix tuwunel --domain test-4.mindroom.chat

./deploy.py start alt
./deploy.py start default
./deploy.py start --only-matrix test
./deploy.py start --only-matrix test-2
./deploy.py start --only-matrix test-3
./deploy.py start --only-matrix test-4
